'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { runCapture, lineBounds, flattenSymbols } = require('./extension');
const range = (a, b, column = 0) => ({ start: { line: a, character: 0 }, end: { line: b, character: column } });
test('exclusive selection boundaries and 400-line limit', () => {
    assert.deepEqual(lineBounds(range(0, 4)), { start: 1, end: 4 });
    assert.deepEqual(lineBounds(range(0, 0, 2)), { start: 1, end: 1 });
    assert.deepEqual(lineBounds(range(0, 400)), { start: 1, end: 400 });
    assert.throws(() => lineBounds(range(0, 400, 1)), /400/);
});
test('nested symbols and foreign SymbolInformation locations', () => {
    const uri = { toString: () => 'file:a' };
    const rows = flattenSymbols([{ name: 'A', range: range(0, 4), children: [{ name: 'B', range: range(1, 2) }] },
        { name: 'foreign', location: { uri: { toString: () => 'file:b' }, range: range(0, 1) } }], uri);
    assert.deepEqual(rows.map(row => row.label), ['A', 'B']);
});
async function fixture(t) {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'tacit editor '));
    t.after(() => fs.rm(root, { recursive: true, force: true }));
    const filename = path.join(root, 'shell $(echo nope).py');
    await fs.writeFile(filename, 'hello\n');
    const calls = [];
    const doc = { uri: { scheme: 'file', fsPath: filename }, version: 1, isDirty: false,
        lineCount: 2, lineAt: () => ({ text: '' }), getText: () => 'hello\n' };
    const vscode = {
        workspace: { isTrusted: true, getConfiguration: () => ({ get: () => '/trusted/tacit executable' }) },
        window: { activeTextEditor: { document: doc, selections: [range(0, 1)], selection: range(0, 1) },
            showInputBox: async () => 'a'.repeat(40), showSaveDialog: async () => ({ scheme: 'file', fsPath: path.join(root, 'context.json') }),
            showInformationMessage: async () => {}, showQuickPick: async items => items[0] },
        commands: { executeCommand: async () => [{ name: 'hello', range: range(0, 1) }] }
    };
    const run = async (executable, args, options) => {
        calls.push({ executable, args, options });
        return { stdout: args.includes('--show-toplevel') ? root + '\n' : 'a'.repeat(40) + '\n' };
    };
    return { vscode, run, calls, doc, root };
}
test('file export uses argv with shell disabled and active Git root', async t => {
    const { vscode, run, calls, root } = await fixture(t);
    await runCapture(vscode, 'file', run);
    const call = calls.at(-1);
    assert.equal(call.executable, '/trusted/tacit executable');
    assert.equal(call.options.shell, false);
    assert.equal(call.options.cwd, root);
    assert.equal(call.args[call.args.indexOf('--file') + 1], 'shell $(echo nope).py');
    assert.equal(call.args[call.args.indexOf('--end-line') + 1], '1');
});
test('symbol picker exports explicit name and boundaries', async t => {
    const { vscode, run, calls } = await fixture(t);
    await runCapture(vscode, 'symbol', run);
    const args = calls.at(-1).args;
    assert.equal(args[args.indexOf('--symbol') + 1], 'hello');
});
for (const kind of ['dirty', 'untrusted', 'external edit', 'multiple selections', 'empty selection', 'changed during prompt']) {
    test(`reject ${kind} before invoking Tacit`, async t => {
        const { vscode, run, calls, doc } = await fixture(t);
        if (kind === 'dirty') doc.isDirty = true;
        if (kind === 'untrusted') vscode.workspace.isTrusted = false;
        if (kind === 'external edit') doc.getText = () => 'stale';
        if (kind === 'multiple selections') vscode.window.activeTextEditor.selections.push(range(0, 1));
        if (kind === 'empty selection') vscode.window.activeTextEditor.selection.isEmpty = true;
        if (kind === 'changed during prompt') vscode.window.showInputBox = async () => { doc.version++; return 'a'.repeat(40); };
        await assert.rejects(() => runCapture(vscode, 'selection', run));
        assert.ok(calls.every(c => c.executable === 'git'));
    });
}
test('cancel output dialog makes no CLI call', async t => {
    const { vscode, run, calls } = await fixture(t);
    vscode.window.showSaveDialog = async () => undefined;
    await runCapture(vscode, 'file', run);
    assert.equal(calls.length, 2);
});

test('adapter to real CLI exports a source-bound symbol without network', async t => {
    const { promisify } = require('node:util');
    const exec = promisify(require('node:child_process').execFile);
    const { vscode, root } = await fixture(t);
    for (const args of [['init', '-q'], ['config', 'user.email', 'fixture@example.invalid'],
        ['config', 'user.name', 'Fixture'], ['add', '.'], ['commit', '-qm', 'fixture']]) {
        await exec('git', args, { cwd: root });
    }
    const commit = (await exec('git', ['rev-parse', 'HEAD'], { cwd: root })).stdout.trim();
    vscode.window.showInputBox = async () => commit;
    vscode.workspace.getConfiguration = () => ({ get: () => path.resolve(__dirname, '../../.venv/bin/tacit') });
    await runCapture(vscode, 'symbol', exec);
    const context = JSON.parse(await fs.readFile(path.join(root, 'context.json'), 'utf8'));
    assert.equal(context.source_commit, commit);
    assert.equal(context.selections[0].symbol, 'hello');
    assert.equal(context.selections[0].dirty_sha256, null);
    assert.equal(context.selections[0].end_line, 1);
});
