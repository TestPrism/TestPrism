import test from 'node:test';
import assert from 'node:assert/strict';
import {failureSignature} from '../src/validation.js';

test('temporary report locations do not turn the same regression into flaky evidence', () => {
  const log = (dir: string) => `FAIL: test_contract (Contract)\nAssertionError: 1 != 0\nrootdir: /tmp/${dir}\n../tmp/${dir}/test_contract.py F [100%]\n/tmp/${dir}/test_contract.py:7: AssertionError\n  File "/tmp/${dir}/test_contract.py", line 7, in test_contract\nE assert False\nRan 1 test in 0.010s`;
  assert.equal(failureSignature('fail', log('first_ab123'), '/testbed'), failureSignature('fail', log('second_cd456'), '/testbed'));
  assert.notEqual(failureSignature('fail', log('first_ab123'), '/testbed'), failureSignature('fail', log('second_cd456').replace('1 != 0', '2 != 0'), '/testbed'));
  assert.notEqual(failureSignature('fail', log('first_ab123'), '/testbed'), failureSignature('fail', log('first_ab123').replaceAll('test_contract.py', 'different_case.py'), '/testbed'));
});

test('temporary paths that are actual expected/observed values remain significant', () => {
  const first = "AssertionError: '/tmp/expected/a' != '/tmp/actual/b'\nE assert output == '/tmp/actual/b'";
  const second = first.replaceAll('/tmp/actual/b', '/tmp/different/c');
  assert.notEqual(failureSignature('fail', first, '/testbed'), failureSignature('fail', second, '/testbed'));
});

test('loopback connection-pool ports do not make the same transport failure flaky', () => {
  const log = (port: number, status = 407) => `requests.exceptions.ProxyError: HTTPSConnectionPool(host='localhost', port=${port}): Max retries exceeded (Tunnel connection failed: ${status} Proxy Authentication Required)\n  File "/testbed/agent-tests/test_transport.py", line 9, in test_request`;
  assert.equal(failureSignature('fail', log(38179), '/testbed'), failureSignature('fail', log(54903), '/testbed'));
  assert.notEqual(failureSignature('fail', log(38179), '/testbed'), failureSignature('fail', log(54903, 403), '/testbed'));
  assert.notEqual(failureSignature('fail', log(38179), '/testbed'), failureSignature('fail', log(38179).replace('ProxyError:', 'TimeoutError:'), '/testbed'));
});

test('ports in assertions, ordinary output and remote pool endpoints remain significant', () => {
  for (const line of [
    "AssertionError: HTTPSConnectionPool(host='localhost', port=38179) != expected",
    "E assert HTTPSConnectionPool(host='localhost', port=38179) == expected",
    "observed HTTPSConnectionPool(host='localhost', port=38179)",
    "ProxyError: HTTPSConnectionPool(host='service.example', port=38179): refused",
  ]) assert.notEqual(failureSignature('fail', line, '/testbed'), failureSignature('fail', line.replace('38179', '54903'), '/testbed'));
});

test('hexadecimal, timing, workspace paths and run identifiers in assertions remain significant', () => {
  for (const [first, second] of [
    ['AssertionError: actual 0x10 != expected 0x00', 'AssertionError: actual 0x20 != expected 0x00'],
    ['AssertionError: elapsed 10ms exceeds 5ms', 'AssertionError: elapsed 900ms exceeds 5ms'],
    ['AssertionError: expected run-first', 'AssertionError: expected run-second'],
    ['AssertionError: /testbed/value != expected', 'AssertionError: /another/value != expected'],
    ['/tmp/case/test.py:7: AssertionError: /tmp/actual/a', '/tmp/case/test.py:7: AssertionError: /tmp/other/a'],
    ['  File "/tmp/case/test.py", line 7: /tmp/actual/a', '  File "/tmp/case/test.py", line 7: /tmp/other/a'],
  ]) assert.notEqual(failureSignature('fail', first!, '/testbed'), failureSignature('fail', second!, '/another'));
});

test('known runner timing and workspace reporting locations normalize without changing assertion suffixes', () => {
  const log = (root: string, time: string) => `  File "${root}/agent-tests/test.py", line 7, in test_case\n`
    + `${root}/agent-tests/test.py:7: AssertionError: 0x10 != 0x00\n`
    + `    at test (${root}/agent-tests/test.cjs:7:3)\n`
    + `    at ${root}/agent-tests/test.cjs:7:3\n`
    + `Ran 1 test in ${time}s\n================ 1 failed, 2 passed in ${time}s ================`;
  const first = log('/tmp/run-a/project', '0.03'), second = log('/tmp/run-b/project', '1.79');
  assert.equal(failureSignature('fail', first, '/tmp/run-a/project'), failureSignature('fail', second, '/tmp/run-b/project'));
  assert.notEqual(failureSignature('fail', first, '/tmp/run-a/project'), failureSignature('fail', second.replace('0x10', '0x20'), '/tmp/run-b/project'));
});
