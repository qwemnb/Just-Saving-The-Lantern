const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const {
    classifyTraceCommand,
    renderTrace,
    setupApp,
    loadMessages,
    TRACE_USAGE,
    NO_MEMORY_RETRIEVAL,
    NETWORK_READ_ERROR,
    HISTORY_READ_FALLBACK,
    DIRECTORY_READ_FALLBACK
} = require('../static/app.js');


class FakeElement {
    constructor(tagName, ownerDocument) {
        this.tagName = tagName.toUpperCase();
        this.ownerDocument = ownerDocument;
        this.children = [];
        this.listeners = new Map();
        this.className = '';
        this.textContent = '';
        this.value = '';
        this.disabled = false;
        this.hidden = false;
        this.inert = false;
        this.parentNode = null;
        this.scrollTop = 0;
        this.scrollHeight = 0;
        this.attributes = new Map();
        this.classList = {
            toggle: name => {
                const names = new Set(this.className.split(/\s+/).filter(Boolean));
                names.has(name) ? names.delete(name) : names.add(name);
                this.className = [...names].join(' ');
                return names.has(name);
            },
            add: name => {
                const names = new Set(this.className.split(/\s+/).filter(Boolean));
                names.add(name);
                this.className = [...names].join(' ');
            }
        };
    }

    appendChild(child) {
        child.parentNode = this;
        this.children.push(child);
        return child;
    }

    replaceChildren(...children) {
        this.children = [];
        children.forEach(child => this.appendChild(child));
        this.textContent = '';
    }

    remove() {
        if (this.parentNode) {
            this.parentNode.children = this.parentNode.children.filter(child => child !== this);
        }
    }

    addEventListener(type, listener) {
        const listeners = this.listeners.get(type) || [];
        listeners.push(listener);
        this.listeners.set(type, listeners);
    }

    setAttribute(name, value) {
        this.attributes.set(name, String(value));
    }

    async dispatch(type, extra = {}) {
        const event = {
            type,
            key: undefined,
            preventDefault() { this.defaultPrevented = true; },
            defaultPrevented: false,
            ...extra
        };
        for (const listener of this.listeners.get(type) || []) {
            await listener(event);
        }
        return event;
    }

    querySelector(selector) {
        if (selector.startsWith('.')) {
            const className = selector.slice(1);
            return walk(this).find(element => element.className.split(/\s+/).includes(className)) || null;
        }
        return null;
    }

    focus() {
        this.ownerDocument.activeElement = this;
    }
}


class FakeDocument {
    constructor() {
        this.elements = new Map();
        this.created = [];
        this.listeners = new Map();
        this.activeElement = null;
    }

    createElement(tagName) {
        const element = new FakeElement(tagName, this);
        this.created.push(element);
        return element;
    }

    register(id, tagName = 'div') {
        const element = this.createElement(tagName);
        element.id = id;
        this.elements.set(id, element);
        return element;
    }

    getElementById(id) {
        return this.elements.get(id) || null;
    }

    addEventListener(type, listener) {
        const listeners = this.listeners.get(type) || [];
        listeners.push(listener);
        this.listeners.set(type, listeners);
    }

    async dispatch(type, extra = {}) {
        const event = {
            type,
            key: undefined,
            preventDefault() { this.defaultPrevented = true; },
            defaultPrevented: false,
            ...extra
        };
        for (const listener of this.listeners.get(type) || []) {
            await listener(event);
        }
        return event;
    }
}


function walk(root) {
    const result = [];
    for (const child of root.children || []) {
        result.push(child, ...walk(child));
    }
    return result;
}


function allText(root) {
    return [root.textContent, ...walk(root).map(element => element.textContent)].join('\n');
}


function makeDocument() {
    const doc = new FakeDocument();
    const form = doc.register('message-form', 'form');
    const input = doc.register('message-input', 'input');
    const send = doc.createElement('button');
    send.className = 'send-button';
    form.appendChild(input);
    form.appendChild(send);
    doc.register('messages');
    doc.register('history-read-status');
    doc.register('app-shell', 'main');
    const overlay = doc.register('trace-overlay');
    overlay.hidden = true;
    doc.register('trace-close', 'button');
    doc.register('trace-panel-status');
    doc.register('trace-command-status');
    doc.register('trace-body');
    return doc;
}


function makeDirectoryDocument() {
    const doc = makeDocument();
    doc.register('destination-button', 'button');
    const picker = doc.register('destination-picker');
    picker.hidden = true;
    doc.register('destination-search', 'input');
    doc.register('destination-options');
    doc.register('participant-list');
    doc.register('participant-panel', 'aside');
    doc.register('participants-toggle', 'button');
    doc.register('participants-refresh', 'button');
    doc.register('participant-read-status');
    return doc;
}


const DIRECTORY = {
    directory_version: 1,
    room: { room_key: 'main', name: 'The Room' },
    destinations: [
        { kind: 'room', label: 'Room', addressable: true },
        { kind: 'participant', participant_key: 'helios', primary_name: 'Helios', aliases: ['Helios', 'Sol'], participant_type: 'ai', addressable: true },
        { kind: 'participant', participant_key: 'peter', primary_name: 'Peter', aliases: ['Peter'], participant_type: 'human', addressable: false }
    ]
};


function response(ok, payload, status = 200) {
    return {
        ok,
        status,
        async json() { return payload; }
    };
}


function minimalTrace(overrides = {}) {
    return {
        trace_version: 2,
        turn: {
            id: 17,
            status: 'open',
            created_at: '2026-08-11T23:00:00.000Z',
            completed_at: null,
            room: { id: 1, room_key: 'main', name: 'The Room' },
            initiated_by: null
        },
        messages: [],
        configurations: [],
        recorded_request: null,
        provider_outcome: null,
        api_events: [],
        ...overrides
    };
}


async function settle() {
    await new Promise(resolve => setImmediate(resolve));
    await new Promise(resolve => setImmediate(resolve));
}


test('browser classifier mirrors exact command grammar and preserves decimal text', () => {
    assert.deepEqual(classifyTraceCommand('/trace'), { kind: 'latest', turnIdText: null });
    assert.deepEqual(classifyTraceCommand(' \t/trace 0009\n'), { kind: 'malformed', turnIdText: null });
    assert.deepEqual(classifyTraceCommand('/trace\n900719925474099312345'), {
        kind: 'turn', turnIdText: '900719925474099312345'
    });
    for (const text of ['/TRACE', '/tracefoo', '/trace/1', 'hello /trace']) {
        assert.equal(classifyTraceCommand(text).kind, 'message');
    }
});


test('valid trace performs one GET, clears input, opens accessibly, and never mutates chat flow', async () => {
    const doc = makeDocument();
    const calls = [];
    const trace = minimalTrace({
        messages: [{
            id: 1,
            participant_key: '<img src=x onerror=alert(1)>',
            message_text: '<script>attack()</script>\n  preserved',
            message_type: 'chat',
            room_sequence_no: 1,
            turn_sequence_no: 1,
            reply_to_id: null,
            reply_to_turn_id: null,
            reply_to_outside_selected_turn: false,
            participant_config_id: null,
            created_at: '2026-08-11T23:00:00.000Z'
        }],
        configurations: [{
            id: 3,
            participant_key: 'helios',
            provider: 'openai',
            model: 'recorded',
            config_label: 'test',
            created_at: '2026-08-11T22:59:00.000Z',
            system_instructions: '  exact instructions\n\t<img src=x onerror=alert(2)>  ',
            settings: { '<img src=x onerror=alert(3)>': '<svg onload=alert(4)>' },
            tools: [],
            omitted_json_pointers: []
        }]
    });
    const fetchStub = async (url, options = {}) => {
        calls.push({ url, options });
        if (url === '/api/messages') return response(true, []);
        if (url === '/api/trace/17') return response(true, trace);
        throw new Error(`Unexpected URL ${url}`);
    };

    setupApp(doc, fetchStub);
    await settle();
    calls.length = 0;
    const input = doc.getElementById('message-input');
    input.value = '/trace 17';
    await doc.getElementById('message-form').dispatch('submit');

    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, '/api/trace/17');
    assert.equal(calls[0].options.method, 'GET');
    assert.equal(input.value, '');
    assert.equal(doc.getElementById('trace-overlay').hidden, false);
    assert.equal(doc.getElementById('app-shell').inert, true);
    assert.equal(doc.activeElement, doc.getElementById('trace-close'));
    assert.doesNotMatch(allText(doc.getElementById('messages')), /Helios is responding/);
    assert.match(allText(doc.getElementById('trace-body')), /<script>attack\(\)<\/script>/);
    assert.match(allText(doc.getElementById('trace-body')), /<img src=x onerror=alert\(2\)>/);
    const preValues = doc.created
        .filter(element => element.tagName === 'PRE')
        .map(element => element.textContent);
    assert.ok(preValues.includes('  exact instructions\n\t<img src=x onerror=alert(2)>  '));
    assert.ok(preValues.some(value => value.includes('<svg onload=alert(4)>')));
    assert.equal(doc.created.some(element => element.tagName === 'SCRIPT'), false);
    assert.equal(doc.created.some(element => element.tagName === 'IMG'), false);
    assert.equal(doc.created.some(element => element.tagName === 'SVG'), false);
    assert.equal(doc.created.some(element => element.className.includes('<img')), false);

    await doc.dispatch('keydown', { key: 'Escape' });
    assert.equal(doc.getElementById('trace-overlay').hidden, true);
    assert.equal(doc.getElementById('app-shell').inert, false);
    assert.equal(doc.activeElement, input);
});


test('latest command and trace failure each make exactly one request without retry or refresh', async () => {
    const doc = makeDocument();
    const calls = [];
    const fetchStub = async (url, options = {}) => {
        calls.push({ url, options });
        if (url === '/api/messages') return response(true, []);
        return response(false, { message: 'Recorded trace unavailable.' }, 503);
    };
    setupApp(doc, fetchStub);
    await settle();
    calls.length = 0;
    const input = doc.getElementById('message-input');
    input.value = '/trace';
    await doc.getElementById('message-form').dispatch('submit');
    await settle();

    assert.deepEqual(calls.map(call => call.url), ['/api/trace/latest']);
    assert.equal(calls[0].options.method, 'GET');
    assert.match(doc.getElementById('trace-panel-status').textContent, /unavailable/);
    assert.doesNotMatch(allText(doc.getElementById('messages')), /unavailable/);
});


test('malformed trace shows usage outside canonical messages and performs no request', async () => {
    const doc = makeDocument();
    const calls = [];
    const fetchStub = async url => {
        calls.push(url);
        return response(true, []);
    };
    setupApp(doc, fetchStub);
    await settle();
    calls.length = 0;
    const input = doc.getElementById('message-input');
    input.value = '/trace 01';
    await doc.getElementById('message-form').dispatch('submit');

    assert.deepEqual(calls, []);
    assert.equal(doc.getElementById('trace-command-status').textContent, TRACE_USAGE);
    assert.doesNotMatch(allText(doc.getElementById('messages')), /Usage: \/trace/);
    assert.equal(input.value, '/trace 01');
});


test('trace rendering handles empty/open data and future recorded memory generically', () => {
    const doc = makeDocument();
    renderTrace(minimalTrace(), doc);
    assert.match(allText(doc.getElementById('trace-body')), new RegExp(NO_MEMORY_RETRIEVAL.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));

    renderTrace(minimalTrace({
        recorded_request: {
            request: {},
            local_context: { memory_retrieval: { source: 'future', value: '<b>text only</b>' } }
        }
    }), doc);
    const text = allText(doc.getElementById('trace-body'));
    assert.match(text, /<b>text only<\/b>/);
    assert.doesNotMatch(text, new RegExp(NO_MEMORY_RETRIEVAL.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
});


test('recorded inherited memory is rendered literally in its dedicated section', () => {
    const doc = makeDocument();
    const hostile = '<img src=x onerror=alert(91)>\n  exact inherited text  ';
    const retrieval = {
        retriever_version: 'seed-fts-topic-v1',
        query_terms: ['glass', 'orchard'],
        result_limit: 5,
        text_budget_chars: 8000,
        selected: [{
            rank: 1,
            seed_memory_id: 7,
            stable_id: '<svg onload=alert(92)>',
            seed_batch_id: 2,
            source_content_sha256: 'a'.repeat(64),
            source_label: 'Synthetic',
            source_locator: 'Fictional locator',
            memory_text_sha256: 'b'.repeat(64),
            exact_topic_match: true,
            topic_match_weight_sum: 1,
            fts_bm25: -1,
            importance: 0.8,
            confidence: 1
        }],
        omitted_for_budget: 0
    };
    renderTrace(minimalTrace({
        inherited_memory: {
            state: 'recorded',
            unavailable_reason: null,
            retrieval,
            context: {
                kind: 'inherited_seed_memory_context',
                records: [{ memory_text: hostile }]
            }
        },
        recorded_request: { request: {}, local_context: { memory_retrieval: retrieval } }
    }), doc);
    const text = allText(doc.getElementById('trace-body'));
    assert.match(text, /Inherited memory/);
    assert.match(text, /seed-fts-topic-v1/);
    assert.match(text, /<img src=x onerror=alert\(91\)>/);
    assert.match(text, /<svg onload=alert\(92\)>/);
    assert.equal(doc.created.some(element => ['IMG', 'SVG', 'SCRIPT'].includes(element.tagName)), false);
    assert.equal(doc.created.some(element => element.className.includes('<svg')), false);
});


test('static dialog declares modal semantics and a visible labelled close control', () => {
    const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
    assert.match(html, /role="dialog"/);
    assert.match(html, /aria-modal="true"/);
    assert.match(html, /aria-labelledby="trace-title"/);
    assert.match(html, /id="trace-close"[^>]*>Close<\/button>/);
    assert.match(html, /id="trace-command-status"/);
});


test('directory initializes Helios, bracket opens searchable picker, and Room POST is structured', async () => {
    const doc = makeDirectoryDocument();
    const calls = [];
    const fetchStub = async (url, options = {}) => {
        calls.push({ url, options });
        if (url === '/api/participants') return response(true, DIRECTORY);
        if (url === '/api/messages' && options.method === 'POST') return response(true, { status: 'completed' });
        if (url === '/api/messages') return response(true, []);
        throw new Error(`Unexpected URL ${url}`);
    };
    setupApp(doc, fetchStub);
    await settle();
    assert.equal(doc.getElementById('destination-button').textContent, 'To: Helios');
    assert.match(allText(doc.getElementById('participant-list')), /Previously: Sol/);
    assert.match(allText(doc.getElementById('participant-list')), /Not addressable/);

    const input = doc.getElementById('message-input');
    input.value = '   ';
    const bracket = await input.dispatch('keydown', { key: '[' });
    assert.equal(bracket.defaultPrevented, true);
    assert.equal(input.value, '   ');
    assert.equal(doc.getElementById('destination-picker').hidden, false);
    assert.equal(doc.activeElement, doc.getElementById('destination-search'));

    const roomOption = doc.getElementById('destination-options').children[0];
    await roomOption.dispatch('click');
    assert.equal(doc.getElementById('destination-button').textContent, 'To: Room');
    input.value = '  exact room text  ';
    await input.dispatch('input');
    calls.length = 0;
    await doc.getElementById('message-form').dispatch('submit');
    const post = calls.find(call => call.options.method === 'POST');
    assert.deepEqual(JSON.parse(post.options.body), {
        message_text: '  exact room text  ',
        destination: { kind: 'room' }
    });
    assert.equal(doc.getElementById('destination-button').textContent, 'To: Room');
});


test('participants command refetches locally and malformed form never posts', async () => {
    const doc = makeDirectoryDocument();
    const calls = [];
    const fetchStub = async (url, options = {}) => {
        calls.push({ url, options });
        if (url === '/api/participants') return response(true, DIRECTORY);
        if (url === '/api/messages') return response(true, []);
        throw new Error(`Unexpected URL ${url}`);
    };
    setupApp(doc, fetchStub);
    await settle();
    calls.length = 0;
    const input = doc.getElementById('message-input');
    input.value = ' /participants ';
    await doc.getElementById('message-form').dispatch('submit');
    assert.deepEqual(calls.map(call => call.url), ['/api/participants']);
    assert.equal(input.value, '');

    calls.length = 0;
    input.value = '/participants secret';
    await doc.getElementById('message-form').dispatch('submit');
    assert.deepEqual(calls, []);
    assert.equal(doc.getElementById('trace-command-status').textContent, 'Usage: /participants');
});


test('history read failures use only local literals and never create message elements', async () => {
    const cases = [
        ['message_history_invalid', 'The message history data is invalid.'],
        ['message_history_unavailable', 'The message history is unavailable.'],
        ['unknown_code', HISTORY_READ_FALLBACK],
        [null, HISTORY_READ_FALLBACK]
    ];
    for (const [code, expected] of cases) {
        const doc = makeDocument();
        const hostile = '<img src=x onerror=alert(404)> SECRET';
        const fetchStub = async () => response(false, {
            ...(code === null ? {} : { error: code }),
            message: hostile
        }, 500);
        assert.equal(await loadMessages(fetchStub, doc), false);
        assert.equal(doc.getElementById('history-read-status').textContent, expected);
        assert.doesNotMatch(allText(doc.getElementById('messages')), /SECRET|img/);
        assert.doesNotMatch(doc.getElementById('history-read-status').textContent, /SECRET|img/);
    }
});


test('invalid JSON, non-JSON, and network history failures use exact safe fallbacks', async () => {
    for (const invalidResponse of [
        { ok: false, async json() { throw new Error('HOSTILE JSON'); } },
        { ok: true, async json() { throw new Error('HOSTILE HTML'); } }
    ]) {
        const doc = makeDocument();
        await loadMessages(async () => invalidResponse, doc);
        assert.equal(doc.getElementById('history-read-status').textContent, HISTORY_READ_FALLBACK);
    }
    const doc = makeDocument();
    await loadMessages(async () => { throw new Error('HOSTILE URL'); }, doc);
    assert.equal(doc.getElementById('history-read-status').textContent, NETWORK_READ_ERROR);
});


test('directory failures are independent, local, and keep sending disabled', async () => {
    const doc = makeDirectoryDocument();
    const hostile = '<script>PRIVATE DIRECTORY</script>';
    const fetchStub = async url => {
        if (url === '/api/messages') {
            return response(false, {
                error: 'message_history_invalid',
                message: 'PRIVATE HISTORY'
            }, 500);
        }
        return response(false, {
            error: 'participant_directory_unavailable',
            message: hostile
        }, 503);
    };
    const controller = setupApp(doc, fetchStub);
    await settle();
    assert.equal(
        doc.getElementById('participant-read-status').textContent,
        'The participant directory is unavailable.'
    );
    assert.equal(
        doc.getElementById('history-read-status').textContent,
        'The message history data is invalid.'
    );
    assert.equal(doc.getElementById('destination-button').disabled, true);
    assert.equal(doc.getElementById('message-form').querySelector('.send-button').disabled, true);
    assert.doesNotMatch(
        [
            doc.getElementById('participant-read-status').textContent,
            doc.getElementById('history-read-status').textContent,
            allText(doc.getElementById('participant-list')),
            allText(doc.getElementById('messages'))
        ].join('\n'),
        /PRIVATE/
    );

    await controller.loadDirectory();
    assert.equal(
        doc.getElementById('history-read-status').textContent,
        'The message history data is invalid.'
    );
});


test('successful endpoint refresh clears only its own read status', async () => {
    const doc = makeDirectoryDocument();
    let directoryFails = true;
    const fetchStub = async url => {
        if (url === '/api/messages') return response(true, []);
        if (directoryFails) {
            return response(false, { error: 'participant_directory_invalid' }, 500);
        }
        return response(true, DIRECTORY);
    };
    const controller = setupApp(doc, fetchStub);
    await settle();
    doc.getElementById('history-read-status').textContent = 'still relevant';
    directoryFails = false;
    await controller.loadDirectory();
    assert.equal(doc.getElementById('participant-read-status').textContent, '');
    assert.equal(doc.getElementById('history-read-status').textContent, 'still relevant');
});


test('static read statuses, cache tokens, and focus-visible selectors are present', () => {
    const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
    const css = fs.readFileSync(path.join(__dirname, '..', 'static', 'style.css'), 'utf8');
    assert.match(html, /id="history-read-status"[^>]*role="status"[^>]*aria-live="polite"/);
    assert.match(html, /id="participant-read-status"[^>]*role="status"[^>]*aria-live="polite"/);
    assert.match(html, /style\.css\?v=schema-preflight-ui-v1/);
    assert.match(html, /app\.js\?v=schema-preflight-ui-v1/);
    assert.match(css, /button:focus-visible/);
    assert.match(css, /input:focus-visible/);
});
