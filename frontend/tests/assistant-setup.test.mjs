import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';

const asURL = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
async function moduleURL(path, imports = {}) {
  const source = await readFile(new URL(path, import.meta.url), 'utf8');
  let { outputText } = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ESNext },
  });
  for (const [name, url] of Object.entries(imports)) {
    outputText = outputText.replaceAll(`'${name}'`, `'${url}'`).replaceAll(`"${name}"`, `"${url}"`);
  }
  return asURL(outputText);
}

const view = await import(await moduleURL('../src/assistantView.ts'));
const sse = await moduleURL('../src/sse.ts');
const api = await import(await moduleURL('../src/api.ts', { './sse': sse }));

test('first-run progress counts ready steps and names the next action', () => {
  const setup = {
    ready: false, state: 'setup_required',
    steps: [
      { key: 'receiver', title: '填写助手 B 的公开抖音号与主页', state: 'ready' },
      { key: 'accounts', title: '登记提问账号 A 与助手 B', state: 'missing' },
      { key: 'device', title: '在本机确认处理设备', state: 'blocked' },
      { key: 'scope', title: '授权可用的资料范围', state: 'missing' },
    ],
  };
  assert.deepEqual(view.assistantSetupProgress(setup), {
    done: 1, total: 4, ready: false, nextTitle: '登记提问账号 A 与助手 B',
  });
  const finished = { ...setup, ready: true, steps: setup.steps.map(step => ({ ...step, state: 'ready' })) };
  assert.deepEqual(view.assistantSetupProgress(finished), { done: 4, total: 4, ready: true, nextTitle: null });
  assert.equal(view.assistantSetupProgress(null).ready, false);
  assert.equal(view.assistantSetupProgress(null).nextTitle, null);
});

test('setup step tones distinguish done, blocked and pending steps', () => {
  assert.equal(view.assistantSetupStepTone('ready'), 'success');
  assert.equal(view.assistantSetupStepTone('blocked'), 'attention');
  assert.equal(view.assistantSetupStepTone('missing'), 'neutral');
  assert.equal(view.assistantSetupStepTone(undefined), 'neutral');
});

test('first-run messages are fixed and never echo unknown input', () => {
  assert.match(api.assistantErrorMessage('assistant_setup_required', 'x'), /请先完成助手账号/);
  assert.match(api.assistantErrorMessage('assistant_receiver_configuration_required', 'x'), /不会自动覆盖/);
  assert.match(api.assistantErrorMessage('assistant_receiver_configuration_invalid', 'x'), /接收器配置无效/);
  assert.match(api.assistantErrorMessage('invalid_receiver_public_number', 'x'), /5–30/);
  assert.match(api.assistantErrorMessage('invalid_receiver_profile_url', 'x'), /douyin\.com\/user/);
  assert.match(api.assistantErrorMessage('receiver_configuration_locked', 'x'), /不会覆盖/);
  assert.equal(api.assistantErrorMessage('synthetic-private-reason', 'fallback'), 'fallback');
});

test('setup actions target only the paired account and local device', () => {
  const setup = {
    ready: false, state: 'setup_required',
    receiver: { state: 'ready', receiver_public_number: '123456', receiver_profile_url: 'https://www.douyin.com/user/B-public' },
    steps: [],
  };
  const unverifiedDevice = {
    device_id: 'd1', instance_id: 'local', is_local: true, enabled: true, revoked: false,
    verification_status: 'unverified', libraries: [], local_owner_access: { active: false },
  };
  const users = [
    { user_id: 'u1', active: true, verification_status: 'verified', sender_profile_url: 'https://www.douyin.com/user/A',
      receiver_profile_url: 'https://www.douyin.com/user/B-public', devices: [unverifiedDevice] },
    { user_id: 'u2', active: true, verification_status: 'verified', sender_profile_url: 'https://www.douyin.com/user/X',
      receiver_profile_url: 'https://www.douyin.com/user/other-B', devices: [] },
  ];
  let context = view.assistantSetupContext(setup, users, 'local');
  assert.equal(context.matchedUser.user_id, 'u1');
  assert.equal(context.localDevice.device_id, 'd1');
  assert.equal(context.verifiedDevice, undefined);
  assert.equal(context.hasScope, false);

  const verifiedUsers = [{ ...users[0], devices: [{ ...unverifiedDevice, verification_status: 'verified',
    libraries: [{ can_query: true, can_import: false }], local_owner_access: { active: false } }] }];
  context = view.assistantSetupContext(setup, verifiedUsers, 'local');
  assert.equal(context.verifiedDevice.device_id, 'd1');
  assert.equal(context.hasScope, true);

  const ownerUsers = [{ ...users[0], devices: [{ ...unverifiedDevice, verification_status: 'verified',
    libraries: [], local_owner_access: { active: true } }] }];
  assert.equal(view.assistantSetupContext(setup, ownerUsers, 'local').hasScope, true);

  const noReceiver = view.assistantSetupContext({ ...setup, receiver: { state: 'missing' } }, users, 'local');
  assert.equal(noReceiver.receiverReady, false);
  assert.equal(noReceiver.matchedUser, undefined);
  assert.equal(view.assistantSetupContext(null, users, 'local').matchedUser, undefined);
});

test('retry-unsent helper only enables never-sent replies', () => {
  const base = { id: 'task', kind: 'clarify', state: 'verification_required', actions: { can_retry_unsent: true } };
  assert.equal(view.canRetryUnsentTask(base), true);
  assert.equal(view.canRetryUnsentTask({ ...base, actions: { can_retry_unsent: false } }), false);
  assert.equal(view.canRetryUnsentTask({ ...base, state: 'b_view_visible' }), false);
  assert.equal(view.canRetryUnsentTask({ ...base, state: 'delivery_unknown' }), false);
  assert.equal(view.canRetryUnsentTask({
    ...base, state: 'needs_review', reason_code: 'original_notification_candidates_exceed_limit',
  }), true);
});

test('native channel stage reflects provisioning and channel state', () => {
  const channel = { configured: true, enabled: false, uid_present: true };
  assert.equal(view.nativeChannelStage(channel, null).label, '接口收件未启用');
  assert.equal(view.nativeChannelStage({ ...channel, enabled: true }, null).label, '接口收件已启用');
  assert.equal(view.nativeChannelStage({ configured: false, enabled: false, uid_present: false }, null).label, '接口收件未配置');
  assert.equal(view.nativeChannelStage(channel, { active: true, stage: 'provisioning' }).label, '切换中');
  assert.equal(view.nativeChannelStage(channel, { active: false, stage: 'provision_failed' }).tone, 'danger');
  assert.equal(view.nativeChannelStage(channel, { active: false, stage: 'enabled' }).label, '接口收件未启用');
});

test('dismiss-unsent helper is bookkeeping only', () => {
  const base = { id: 'task', kind: 'clarify', state: 'needs_review', actions: { can_dismiss_unsent: true } };
  assert.equal(view.canDismissUnsentTask(base), true);
  assert.equal(view.canDismissUnsentTask({ ...base, state: 'b_view_visible' }), false);
  assert.equal(view.canDismissUnsentTask({ ...base, actions: { can_dismiss_unsent: false } }), false);
});

test('readback-unsent helper is read-only gated', () => {
  const base = { id: 'task', kind: 'clarify', state: 'verification_required', actions: { can_readback_unsent: true } };
  assert.equal(view.canReadbackUnsentTask(base), true);
  assert.equal(view.canReadbackUnsentTask({ ...base, state: 'b_view_visible' }), false);
  assert.equal(view.canReadbackUnsentTask({ ...base, actions: { can_readback_unsent: false } }), false);
});

test('retry-after-readback helper requires fresh readback reason', () => {
  const base = { id: 'task', kind: 'clarify', state: 'verification_required', reason_code: 'readback_confirmed_unsent', actions: { can_retry_after_readback: true } };
  assert.equal(view.canRetryAfterReadbackTask(base), true);
  assert.equal(view.canRetryAfterReadbackTask({ ...base, reason_code: null }), false);
  assert.equal(view.canRetryAfterReadbackTask({ ...base, state: 'b_view_visible' }), false);
  assert.equal(view.canRetryAfterReadbackTask({ ...base, actions: { can_retry_after_readback: false } }), false);
});

test('task guidance gives one coherent next step', () => {
  const waiting = { id: 't', kind: 'clarify', state: 'waiting_library', reply_state: 'b_view_visible', actions: {} };
  assert.equal(view.taskGuidance(waiting).tone, 'success');
  assert.deepEqual(view.taskGuidance(waiting).actions, []);
  assert.equal(view.taskGuidance({ ...waiting, state: 'b_view_visible' }).actions.length, 0);

  const verification = { id: 't', kind: 'clarify', state: 'verification_required', reply_state: 'verification_required',
    actions: { can_readback_unsent: true, can_dismiss_unsent: true } };
  assert.equal(view.taskGuidance(verification).tone, 'attention');
  assert.deepEqual(view.taskGuidance(verification).actions, ['readback_unsent', 'dismiss_unsent']);

  const afterReadback = { ...verification, reason_code: 'readback_confirmed_unsent',
    actions: { can_retry_after_readback: true, can_dismiss_unsent: true } };
  assert.deepEqual(view.taskGuidance(afterReadback).actions, ['retry_after_readback', 'dismiss_unsent']);

  const unknown = { id: 't', kind: 'clarify', state: 'delivery_unknown', reply_state: 'delivery_unknown',
    actions: { can_reconcile: true } };
  assert.equal(view.taskGuidance(unknown).tone, 'danger');
  assert.deepEqual(view.taskGuidance(unknown).actions, ['reconcile']);
});

test('exhausted readback guidance stops offering a resend', () => {
  const task = { id: 't', kind: 'clarify', state: 'verification_required', reply_state: 'verification_required',
    reason_code: 'readback_confirmed_unsent',
    actions: { can_dismiss_unsent: true, can_readback_unsent: true, can_retry_after_readback: false } };
  const guidance = view.taskGuidance(task);
  assert.equal(guidance.tone, 'attention');
  assert.match(guidance.title, /已尝试重新发表/);
  assert.deepEqual(guidance.actions, ['dismiss_unsent']);
});
