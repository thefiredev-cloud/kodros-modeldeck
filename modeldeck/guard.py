"""All mutation flows through the existing gated helper, never the raw /swap API."""
import json
import os
import re
import secrets
import subprocess
import threading
import time
from pathlib import Path


class GuardError(ValueError):
    pass


class SwapGuard:
    def __init__(self, state, execute, gate, pending_file=None):
        self.state = state
        self.execute = execute
        self.gate = gate
        self.tickets = {}
        self.lock = threading.Lock()
        self.pending_file = Path(pending_file) if pending_file else None
        self.pending_job = None
        if self.pending_file and self.pending_file.exists():
            try:
                self.pending_job = json.loads(self.pending_file.read_text()).get('job') or 'unknown'
            except (OSError, ValueError, AttributeError):
                self.pending_job = 'unknown'

    def eligible(self, profile, state):
        if self.pending_job:
            outcome = state.get('action_job') or {}
            if outcome.get('job') == self.pending_job and outcome.get('terminal') is True and state.get('swap_state') == 'idle':
                if self.pending_file:
                    self.pending_file.unlink(missing_ok=True)
                self.pending_job = None
            else:
                if self.pending_job == 'unknown':
                    raise GuardError('Swap admission record is unreadable. Inspect ' + str(self.pending_file) + '; no job will be submitted.')
                raise GuardError('A gated job is pending or its outcome is unknown. Inspect the cluster job before retrying.')
        if not isinstance(profile, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,47}', profile):
            raise GuardError('Invalid profile identifier.')
        if not all(state.get(k) for k in ['cluster_online', 'profiles_fresh', 'gated_swap_available']):
            raise GuardError('Live cluster, fresh profiles and verified gated helper are required.')
        if state.get('swap_state') != 'idle':
            raise GuardError('The controller is busy or its state is unknown.')
        if not isinstance(state.get('current'), str) or not state['current']:
            raise GuardError('The resident profile is unknown.')
        if profile == state.get('current'):
            raise GuardError('This profile is already current.')
        if profile not in [p['id'] for p in state.get('profiles', [])]:
            raise GuardError('This profile is not in the live registry.')

    def prepare(self, profile):
        with self.lock:
            state = self.state()
            self.eligible(profile, state)
            now = time.monotonic()
            self.tickets = {k: v for k, v in self.tickets.items() if v['expires'] > now}
            ticket = secrets.token_urlsafe(32)
            phrase = 'SWAP ' + profile + ' WITH 3.5 MINUTE OUTAGE'
            self.tickets[ticket] = {'profile': profile, 'current': state.get('current'), 'expires': now + 60, 'phrase': phrase}
            return {'ticket': ticket, 'confirmation': phrase, 'expires_in_s': 60,
                    'warning': 'Every cluster session will lose inference for about 3.5 minutes. Actual outage can be longer. Gates may roll back.'}

    def commit(self, ticket, phrase):
        with self.lock:
            item = self.tickets.pop(ticket, None) if isinstance(ticket, str) else None
            if not item or time.monotonic() >= item['expires'] or phrase != item['phrase']:
                raise GuardError('Confirmation expired, invalid or already used. Review the swap again.')
            state = self.state()
            self.eligible(item['profile'], state)
            if state.get('current') != item['current']:
                raise GuardError('The resident profile changed. Review the swap again.')
            if not self.gate(state):
                raise GuardError('Jev denied or could not verify the safety gate. No swap was sent.')
            job = 'modeldeck-swap-' + secrets.token_hex(6)
            # Persist admission BEFORE transport. A crash or SSH timeout cannot erase the latch.
            if self.pending_file:
                record = json.dumps({'job': job, 'profile': item['profile'], 'from': state.get('current'), 'submitted_at': time.time()})
                try:
                    with self.pending_file.open('x') as stream:
                        self.pending_file.chmod(0o600)
                        stream.write(record)
                        stream.flush()
                        import os
                        os.fsync(stream.fileno())
                except OSError as exc:
                    raise GuardError('Could not persist exclusive swap admission. No job was sent.') from exc
            self.pending_job = job
            self.tickets.clear()
            return self.execute(item['profile'], job)


def jev_gate(state):
    try:
        result = subprocess.run([os.environ.get('MODELDECK_JEV_BIN', 'jev'), 'if',
            'Is it safe to submit this explicitly two-step-confirmed profile swap through the existing idle, rollback and acceptance-gated swap helper?',
            '--state', '-'], input=json.dumps(state), capture_output=True, text=True, timeout=20, check=False)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def execute_gated(profile, job=None):
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,47}', profile):
        raise GuardError('Invalid profile identifier.')
    job = job or 'modeldeck-swap-' + secrets.token_hex(6)
    if not re.fullmatch(r'modeldeck-swap-[0-9a-f]{12}', job):
        raise GuardError('Invalid gated job identifier.')
    # Never bypass this helper with the gateway's raw passthrough route.
    command = ('systemd-run --user --unit=' + job + ' --property=RemainAfterExit=yes '
               '--property=StandardOutput=journal --property=StandardError=journal '
               '%h/ops-cluster-improve-exec/bin/swap-gate swap ' + profile)
    try:
        result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=4', 'cluster-a', command],
                                text=True, capture_output=True, timeout=10, check=False)
        if result.returncode:
            raise GuardError('The gated job was not accepted. Check the cluster controller and admission record.')
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GuardError('Submission outcome unknown. Inspect the cluster journal for ' + job + ' before retrying.') from exc
    return {'status': 'submitted', 'job': job, 'profile': profile,
            'message': 'Gated job submitted, not a successful swap. Follow controller history and the cluster journal.'}
