const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {webcrypto, createHash} = require('node:crypto');

function page() {
    const elements = new Map(), events = [];
    const element = id => {
        if (!elements.has(id)) elements.set(id, {
            disabled: false, textContent: '',
            classList: {toggle(name, enabled) {this[name] = enabled;}},
        });
        return elements.get(id);
    };
    const context = vm.createContext({
        document: {
            addEventListener() {}, getElementById: element,
            createElement: () => ({click() {events.push('download');}, remove() {}}),
            body: {appendChild() {}},
        },
        Headers, Uint8Array, crypto: webcrypto,
        URL: {createObjectURL: () => 'blob:test', revokeObjectURL() {}},
        setTimeout() {}, clearTimeout() {}, setInterval() {},
        sessionStorage: {getItem() {return 'admin';}},
        fetch: async (url, options) => {
            events.push({url, body: JSON.parse(options.body)});
            return {ok: true, status: 200, json: async () => ({})};
        },
    });
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../agribank_recon/static/app.js'), 'utf8'), context);
    return {context, elements, events};
}

test('acknowledgement uses the complete CSV hash after initiating its download', async () => {
    const {context, events} = page();
    const payload = Buffer.from('\ufeffrecord_type,format_version\r\nMANIFEST,1\r\n');
    const expected = createHash('sha256').update(payload).digest('hex');
    const response = {
        ok: true, headers: new Headers({'X-Suspension-ID': 'saved-id', 'X-Reconciliation-Run-ID': 'run-id'}),
        blob: async () => new Blob([payload]),
    };
    await context.saveSuspensionResponse(response);
    assert.equal(events[0], 'download');
    assert.equal(events[1].url, '/api/suspensions/saved-id/acknowledge');
    assert.equal(events[1].body.csv_hash, expected);
    assert.equal(events[1].body.run_id, 'run-id');
});

test('an interrupted CSV response never acknowledges or unlocks urgent upload', async () => {
    const {context, events} = page();
    await assert.rejects(() => context.saveSuspensionResponse({
        ok: true, headers: new Headers(), blob: async () => {throw new Error('download interrupted');},
    }), /download interrupted/);
    assert.equal(events.length, 0);
});

test('urgent button stays locked until a ready snapshot, and operator sees no admin controls', () => {
    const {context, elements} = page();
    vm.runInContext(`activeWorkflow = {run_id:'run-id', suspended:true, export_ready:false,
        can_upload:false, can_suspend:false, can_resume:true, can_close:false,
        unprocessed_count:1, pending_count:1}; renderWorkflow();`, context);
    assert.equal(elements.get('urgent-upload-button').disabled, true);
    assert.equal(elements.get('upload-run-button').disabled, true);
    vm.runInContext('activeWorkflow.export_ready=true; activeWorkflow.can_upload=true; renderWorkflow();', context);
    assert.equal(elements.get('urgent-upload-button').disabled, false);
    assert.equal(elements.get('upload-run-button').disabled, true);
    vm.runInContext("currentUserRole='operator'; applyRoleVisibility();", context);
    for (const id of ['upload-run-button', 'close-run-button', 'suspend-run-button', 'urgent-upload-button', 'suspensions-panel']) {
        assert.equal(elements.get(id).classList['d-none'], true, id);
    }
});
