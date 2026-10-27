import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

root = Path(os.environ['RUNNER_TEMP']) / 'pr1362-validation'
root.mkdir(exist_ok=True)
src = Path.cwd()
env = os.environ.copy()
go = 'go'
report = {'base_head': 'bcac0f021436b4f59a592a134a0c7ab4224f8c7d', 'status': 'running', 'steps': [],
          'toolchain': subprocess.check_output([go, 'version'], env=env).decode().strip()}
report_path = root / 'validation.json'
handler = src / 'policy/handler.go'
original = handler.read_text()
tracked = ['policy/handler.go', 'policy/handler_test.go', 'policy/cooldown_restore_regression_test.go', 'go.mod', 'go.sum']
report['input_sha256'] = {name: hashlib.sha256((src/name).read_bytes()).hexdigest() for name in tracked}

def save():
    temp = report_path.with_suffix('.tmp')
    temp.write_text(json.dumps(report, indent=2))
    temp.replace(report_path)

def run(name, args, expected_failure=False, marker=None):
    step = {'name': name, 'args': args, 'status': 'running',
            'started_at': datetime.datetime.utcnow().isoformat() + 'Z'}
    report['steps'].append(step)
    save()
    started = time.monotonic()
    log_path = root / (name + '.log')
    with log_path.open('w') as log:
        try:
            child = subprocess.Popen([go] + args, cwd=str(src), env=env, stdout=log, stderr=subprocess.STDOUT)
            step['pid'] = child.pid
            save()
            code = child.wait(timeout=1200)
        except subprocess.TimeoutExpired:
            child.terminate()
            child.wait(timeout=15)
            raise RuntimeError(name + ' exceeded compile/test time limit')
    output = log_path.read_text()
    print(name + ':\n' + output, flush=True)
    passed = code == 0 if not expected_failure else code != 0 and marker in output and 'build failed' not in output
    step.update({'exit_code': code, 'duration_seconds': round(time.monotonic()-started, 3),
                 'status': 'passed' if passed else 'failed', 'expected_failure': expected_failure,
                 'ended_at': datetime.datetime.utcnow().isoformat() + 'Z', 'output_tail': output[-5000:]})
    save()
    print(json.dumps(step), flush=True)
    if not passed:
        raise RuntimeError(name + ' did not meet its expected result')

save()
try:
    pattern = '^TestNewPolicyHandler(RestoresCooldownDeadline|WithoutPositiveLastEventStaysIdle|RestoredCooldownGatesRun|HasNoStaleRestoreTimer)$'
    run('focused-race-repeat', ['test', '-mod=readonly', '-p=1', '-race', '-count=100', '-shuffle=on', '-timeout=120s', '-run', pattern, './policy'])
    run('policy-suite', ['test', '-mod=readonly', '-p=1', '-count=1', '-timeout=180s', './policy'])
    run('policy-race', ['test', '-mod=readonly', '-p=1', '-race', '-count=1', '-timeout=180s', './policy'])
    run('policy-vet', ['vet', '-mod=readonly', '-p=1', './policy'])

    deadline = 'h.updateOutOfCooldownOn(time.Unix(0, lastEventTS).Add(h.policy.CooldownOnScaleUp))'
    assert original.count(deadline) == 1
    handler.write_text(original.replace(deadline, 'time.AfterFunc(time.Unix(0, lastEventTS).Add(h.policy.CooldownOnScaleUp).Sub(nowFunc()), func() { h.updateState(StateIdle) })'))
    run('red-original-timer', ['test', '-mod=readonly', '-p=1', '-count=1', '-timeout=60s', '-run', pattern, './policy'], True, 'restored cooldown deadline=')
    handler.write_text(original.replace(deadline, 'h.updateOutOfCooldownOn(time.Unix(0, lastEventTS).Add(h.policy.Cooldown))'))
    run('red-wrong-cooldown-field', ['test', '-mod=readonly', '-p=1', '-count=1', '-timeout=60s', '-run', '^TestNewPolicyHandlerRestoresCooldownDeadline/(shorter_scale_up|longer_scale_up)$', './policy'], True, 'restored cooldown deadline=')
    handler.write_text(original.replace('if nowFunc().After(h.getOutOfCooldownOn()) &&', 'if false && nowFunc().After(h.getOutOfCooldownOn()) &&'))
    run('red-expiry-disabled', ['test', '-mod=readonly', '-p=1', '-count=1', '-timeout=60s', '-run', '^TestNewPolicyHandlerRestoredCooldownGatesRun$', './policy'], True, 'did not allow Scale after its deadline')
    handler.write_text(original)
    run('restored-focused', ['test', '-mod=readonly', '-p=1', '-count=1', '-timeout=60s', '-run', pattern, './policy'])
    report['status'] = 'passed'
except Exception as error:
    report['status'] = 'failed'
    report['error'] = str(error)
finally:
    handler.write_text(original)
    report['final_sha256'] = {name: hashlib.sha256((src/name).read_bytes()).hexdigest() for name in tracked}
    report['sources_restored'] = report['input_sha256'] == report['final_sha256']
    save()

print(json.dumps(report, indent=2), flush=True)
raise SystemExit(0 if report['status'] == 'passed' and report['sources_restored'] else 1)
