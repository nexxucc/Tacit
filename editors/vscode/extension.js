'use strict';
const path = require('node:path');
const fs = require('node:fs/promises');
const crypto = require('node:crypto');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const execute = promisify(execFile);

function lineBounds(range) {
    // VS Code's end is exclusive; selecting to column zero excludes that line.
    const end = range.end.line + (range.end.character === 0 && range.end.line > range.start.line ? 0 : 1);
    const start = range.start.line + 1;
    if (end - start >= 400) throw new Error('Select at most 400 source lines.');
    return { start, end };
}
function flattenSymbols(symbols, uri) {
    const result = [];
    for (const symbol of symbols || []) {
        const range = symbol.range || (symbol.location?.uri.toString() === uri.toString() ? symbol.location.range : null);
        if (range) result.push({ label: symbol.name, description: `line ${range.start.line + 1}`, range });
        if (symbol.children) result.push(...flattenSymbols(symbol.children, uri));
    }
    return result;
}
async function runCapture(vscode, kind, run = execute) {
    if (!vscode.workspace.isTrusted) throw new Error('Trust this workspace before running Tacit.');
    const editor = vscode.window.activeTextEditor;
    if (!editor || editor.document.uri.scheme !== 'file') throw new Error('Open a saved local source file.');
    const doc = editor.document;
    if (doc.isDirty || doc.isUntitled) throw new Error('Save this file before exporting context.');
    if (doc.encoding && doc.encoding !== 'utf8' && doc.encoding !== 'utf8bom') throw new Error('Use a UTF-8 source file.');
    const version = doc.version;
    const filename = doc.uri.fsPath;
    // Discover the Git root from the active file, including nested and multi-root workspaces.
    const root = (await run('git', ['-C', path.dirname(filename), 'rev-parse', '--show-toplevel'], { timeout: 10000 })).stdout.trim();
    const relative = path.relative(root, filename).split(path.sep).join('/');
    if (!relative || relative.startsWith('../') || path.isAbsolute(relative)) throw new Error('Source file is outside the checkout.');
    const head = (await run('git', ['-C', root, 'rev-parse', 'HEAD'], { timeout: 10000 })).stdout.trim();
    let bounds;
    let name;
    if (kind === 'symbol') {
        const symbols = await vscode.commands.executeCommand('vscode.executeDocumentSymbolProvider', doc.uri);
        const chosen = await vscode.window.showQuickPick(flattenSymbols(symbols, doc.uri), { placeHolder: 'Choose a symbol (at most 400 lines)' });
        if (!chosen) return;
        bounds = lineBounds(chosen.range);
        name = chosen.label;
    } else if (kind === 'selection') {
        if (editor.selection.isEmpty) throw new Error('Select source lines first.');
        if (editor.selections.length !== 1) throw new Error('Use one contiguous selection.');
        bounds = lineBounds(editor.selection);
    } else {
        const end = doc.lineCount > 1 && doc.lineAt(doc.lineCount - 1).text === '' ? doc.lineCount - 1 : doc.lineCount;
        bounds = { start: 1, end };
        if (end > 400) throw new Error('File exceeds 400 lines; select lines or a symbol instead.');
    }
    const commit = await vscode.window.showInputBox({ title: 'Pin a pushed source commit', value: head,
        prompt: 'Full 40-character SHA. Local export cannot prove this commit is pushed; Tacit validates it during teaching.',
        validateInput: value => /^[0-9a-f]{40}$/.test(value) ? undefined : 'Enter a full lowercase 40-character SHA.' });
    if (!commit) return;
    if (!/^[0-9a-f]{40}$/.test(commit)) throw new Error('Invalid source commit.');
    const output = await vscode.window.showSaveDialog({ title: 'Export private Tacit editor context', filters: { JSON: ['json'] } });
    if (!output) return;
    if (output.scheme !== 'file') throw new Error('Choose a local output file.');
    if (doc.isDirty || doc.version !== version) throw new Error('Source changed during selection; export again.');
    if ((await fs.stat(filename)).size > 65536) throw new Error('Editor context supports files up to 64 KiB.');
    const bytes = await fs.readFile(filename);
    // Reject external edits not yet reflected in the editor buffer. Preserve an optional UTF-8 BOM.
    if (bytes.toString('utf8').replace(/^\uFEFF/, '') !== doc.getText().replace(/^\uFEFF/, '')) throw new Error('Editor and disk differ; reload the file.');
    const hash = crypto.createHash('sha256').update(bytes).digest('hex');
    const args = ['editor', 'capture', '--root', root, '--commit', commit, '--file', relative,
        '--start-line', String(bounds.start), '--end-line', String(bounds.end), '--expected-sha256', hash, '--output', output.fsPath];
    if (name) args.push('--symbol', name);
    const executable = vscode.workspace.getConfiguration('tacit').get('cliPath', 'tacit');
    await run(executable, args, { cwd: root, timeout: 30000, maxBuffer: 131072, shell: false });
    await vscode.window.showInformationMessage('Tacit context exported locally. Review before teaching; nothing was uploaded or published.');
}
function activate(context) {
    const vscode = require('vscode');
    for (const [command, kind] of [['tacit.captureFile', 'file'], ['tacit.captureSelection', 'selection'], ['tacit.captureSymbol', 'symbol']]) {
        context.subscriptions.push(vscode.commands.registerCommand(command, async () => {
            try { await runCapture(vscode, kind); }
            catch (error) { vscode.window.showErrorMessage(`Tacit: ${error.message}`); }
        }));
    }
}
module.exports = { activate, runCapture, lineBounds, flattenSymbols };
