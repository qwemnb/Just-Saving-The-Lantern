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
    DIRECTORY_READ_FALLBACK,
    POST_ERROR_FALLBACK,
    sendMessage,
    PARTICIPANT_COLOR_STORAGE_KEY,
    DEFAULT_PARTICIPANT_COLORS,
    NEUTRAL_PARTICIPANT_COLOR,
    normalizeParticipantColor,
    loadParticipantColorOverrides,
    createParticipantColorPreferences,
    authorColorKey,
    participantColorForAuthor,
    displayMessages,
    showSystemMessage
} = require('../static/app.js');


class FakeStyle {
    constructor() {
        this.values = new Map();
    }

    setProperty(name, value) {
        this.values.set(name, String(value));
    }

    getPropertyValue(name) {
        return this.values.get(name) || '';
    }
}


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
        this.style = new FakeStyle();
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

    getAttribute(name) {
        return this.attributes.has(name) ? this.attributes.get(name) : null;
    }

    async dispatch(type, extra = {}) {
        const event = {
            type,
            key: undefined,
            preventDefault() { this.defaultPrevented = true; },
            stopPropagation() { this.propagationStopped = true; },
            defaultPrevented: false,
            propagationStopped: false,
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
    const responseControl = doc.register('response-destination-control', 'label');
    responseControl.hidden = true;
    doc.register('response-destination-select', 'select');
    return doc;
}


const DIRECTORY = {
    directory_version: 1,
    room: { room_key: 'main', name: 'The Room' },
    destinations: [
        { kind: 'room', label: 'Room', addressable: true },
        { kind: 'participant', participant_key: 'gemini', primary_name: 'Gemini', aliases: ['Gemini'], participant_type: 'ai', addressable: true },
        { kind: 'participant', participant_key: 'helios', primary_name: 'Helios', aliases: ['Helios', 'Sol'], participant_type: 'ai', addressable: true },
        { kind: 'participant', participant_key: 'peter', primary_name: 'Peter', aliases: ['Peter'], participant_type: 'human', addressable: false }
    ]
};


class SyntheticStorage {
    constructor(raw = null, { throwOnGet = false, throwOnSet = false } = {}) {
        this.raw = raw;
        this.throwOnGet = throwOnGet;
        this.throwOnSet = throwOnSet;
        this.getCalls = [];
        this.setCalls = [];
    }

    getItem(key) {
        this.getCalls.push(key);
        if (this.throwOnGet) throw new Error('synthetic get failure');
        return this.raw;
    }

    setItem(key, value) {
        this.setCalls.push([key, value]);
        if (this.throwOnSet) throw new Error('synthetic set failure');
        this.raw = value;
    }
}


function response(ok, payload, status = 200) {
    return {
        ok,
        status,
        async json() { return payload; }
    };
}


function minimalTrace(overrides = {}) {
    return {
        trace_version: 3,
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


function syntheticMessage(author, destination = 'Helios', text = `${author} message`) {
    return {
        participant_key: author,
        message_text: text,
        created_at: '2026-08-17T12:00:00.000Z',
        routing: {
            sender: { participant_key: author, display_name: author },
            destination: { kind: 'participant', display_name: destination }
        }
    };
}


function participantRow(doc, label) {
    return doc.getElementById('participant-list').children.find(row => {
        const button = row.querySelector('.participant-entry');
        return button && button.children[0] && button.children[0].textContent === label;
    });
}


test('participant color constants, normalization, and author mapping are closed and exact', () => {
    assert.deepEqual(DEFAULT_PARTICIPANT_COLORS, {
        room: '#7C5CC4',
        gemini: '#4F8FEA',
        helios: '#D39A2C',
        peter: '#2F9E8F'
    });
    assert.equal(NEUTRAL_PARTICIPANT_COLOR, '#D8D8E0');
    assert.equal(normalizeParticipantColor('#8b5cf6'), '#8B5CF6');
    for (const invalid of ['8B5CF6', '#ABC', '#ABCDEG', '#1234567', '', null, 7]) {
        assert.equal(normalizeParticipantColor(invalid), null);
    }
    assert.equal(authorColorKey('peter'), 'peter');
    assert.equal(authorColorKey('helios'), 'helios');
    assert.equal(authorColorKey('gemini'), 'gemini');
    assert.equal(authorColorKey('room-system'), 'room');
    assert.equal(authorColorKey('future-participant'), null);
    assert.equal(participantColorForAuthor('future-participant'), '#D8D8E0');
});


test('participant color storage loads valid entries independently and fails safely', () => {
    assert.equal(PARTICIPANT_COLOR_STORAGE_KEY, 'helios-room.participant-colors.v1');
    for (const raw of [null, '{broken', 'null', '[]', '"text"', '42']) {
        assert.deepEqual(loadParticipantColorOverrides(new SyntheticStorage(raw)), {});
    }
    const storage = new SyntheticStorage(JSON.stringify({
        room: '#123abc',
        gemini: 'blue',
        helios: '#112233',
        peter: '#ABCDEF',
        future: '#000000'
    }));
    assert.deepEqual(loadParticipantColorOverrides(storage), {
        room: '#123ABC',
        helios: '#112233',
        peter: '#ABCDEF'
    });
    assert.deepEqual(storage.getCalls, [PARTICIPANT_COLOR_STORAGE_KEY]);
    assert.deepEqual(
        loadParticipantColorOverrides(new SyntheticStorage(null, { throwOnGet: true })),
        {}
    );
});


test('participant color writes retain valid memory, reject invalid input, and reset overrides', () => {
    const storage = new SyntheticStorage(null);
    const preferences = createParticipantColorPreferences(storage);
    assert.deepEqual(preferences.colors, { ...DEFAULT_PARTICIPANT_COLORS });
    assert.equal(preferences.setColor('gemini', '#a1b2c3'), true);
    assert.equal(preferences.colors.gemini, '#A1B2C3');
    assert.deepEqual(JSON.parse(storage.raw), { gemini: '#A1B2C3' });
    const writes = storage.setCalls.length;
    assert.equal(preferences.setColor('gemini', 'not-a-color'), false);
    assert.equal(preferences.colors.gemini, '#A1B2C3');
    assert.equal(storage.setCalls.length, writes);
    assert.equal(preferences.resetColor('gemini'), true);
    assert.equal(preferences.colors.gemini, '#4F8FEA');
    assert.deepEqual(JSON.parse(storage.raw), {});

    const failing = new SyntheticStorage(null, { throwOnSet: true });
    const inMemory = createParticipantColorPreferences(failing);
    assert.equal(inMemory.setColor('peter', '#010203'), true);
    assert.equal(inMemory.colors.peter, '#010203');
    assert.equal(failing.setCalls.length, 1);
});


test('canonical message accents use authors while unknown authors and system notices stay neutral', () => {
    const doc = makeDocument();
    const colors = { ...DEFAULT_PARTICIPANT_COLORS, peter: '#010203', helios: '#AABBCC' };
    displayMessages([
        syntheticMessage('peter', 'Helios'),
        syntheticMessage('helios', 'Peter'),
        syntheticMessage('peter', 'Gemini'),
        syntheticMessage('gemini', 'Peter'),
        syntheticMessage('room-system', 'Room'),
        syntheticMessage('future-participant', 'Room')
    ], doc, colors);
    const rendered = doc.getElementById('messages').children;
    assert.deepEqual(rendered.map(message => message.style.getPropertyValue('--participant-color')), [
        '#010203', '#AABBCC', '#010203', '#4F8FEA', '#7C5CC4', '#D8D8E0'
    ]);
    assert.deepEqual(rendered.map(message => message.getAttribute('data-participant-color-key')), [
        'peter', 'helios', 'peter', 'gemini', 'room', 'neutral'
    ]);
    const system = showSystemMessage('Local notice', doc);
    assert.equal(system.className, 'message system');
    assert.equal(system.style.getPropertyValue('--participant-color'), '#D8D8E0');
});


test('participant rows keep destination and accessible color controls independent', async () => {
    const doc = makeDirectoryDocument();
    const storage = new SyntheticStorage(null);
    const calls = [];
    const fetchStub = async url => {
        calls.push(url);
        if (url === '/api/participants') return response(true, DIRECTORY);
        if (url === '/api/messages') return response(true, []);
        throw new Error(`Unexpected synthetic URL ${url}`);
    };
    setupApp(doc, fetchStub, storage);
    await settle();
    assert.deepEqual(calls.sort(), ['/api/messages', '/api/participants']);

    for (const [label, color, disabled] of [
        ['Room', '#7C5CC4', false],
        ['Gemini', '#4F8FEA', false],
        ['Helios', '#D39A2C', false],
        ['Peter', '#2F9E8F', true]
    ]) {
        const row = participantRow(doc, label);
        const destination = row.querySelector('.participant-entry');
        const swatch = row.querySelector('.participant-color-swatch');
        assert.equal(destination.disabled, disabled);
        assert.equal(swatch.disabled, false);
        assert.equal(swatch.parentNode, row);
        assert.notEqual(swatch.parentNode, destination);
        assert.equal(swatch.getAttribute('aria-label'), `Choose color for ${label}`);
        assert.equal(swatch.style.getPropertyValue('--participant-color'), color);
    }
    const roomRow = participantRow(doc, 'Room');
    await roomRow.querySelector('.participant-entry').dispatch('click');
    assert.equal(doc.getElementById('destination-button').textContent, 'Ask: Room');
    const peterRow = participantRow(doc, 'Peter');
    assert.match(allText(peterRow), /Not addressable/);
    const before = doc.getElementById('destination-button').textContent;
    const peterSwatch = peterRow.querySelector('.participant-color-swatch');
    await peterSwatch.dispatch('click');
    assert.equal(doc.getElementById('destination-button').textContent, before);
    assert.equal(peterSwatch.getAttribute('aria-expanded'), 'true');
    const peterHex = peterRow.querySelector('.participant-color-hex');
    peterHex.value = '#123456';
    await peterHex.dispatch('input');
    assert.equal(peterSwatch.style.getPropertyValue('--participant-color'), '#123456');
    assert.equal(doc.getElementById('destination-button').textContent, 'Ask: Room');
    assert.equal(peterRow.querySelector('.participant-entry').disabled, true);
});


test('color editor synchronizes valid input, preserves invalid state, resets, and restores focus', async () => {
    const doc = makeDirectoryDocument();
    const storage = new SyntheticStorage(null);
    const messages = [
        syntheticMessage('gemini', 'Peter'),
        syntheticMessage('peter', 'Gemini'),
        syntheticMessage('room-system', 'Room')
    ];
    const fetchStub = async url => url === '/api/participants'
        ? response(true, DIRECTORY)
        : response(true, messages);
    const controller = setupApp(doc, fetchStub, storage);
    await settle();

    const geminiRow = participantRow(doc, 'Gemini');
    const swatch = geminiRow.querySelector('.participant-color-swatch');
    const editor = geminiRow.querySelector('.participant-color-editor');
    const picker = editor.querySelector('.participant-color-picker');
    const hex = editor.querySelector('.participant-color-hex');
    const reset = editor.querySelector('.participant-color-reset');
    const close = editor.querySelector('.participant-color-close');
    assert.equal(picker.getAttribute('aria-label'), 'Color picker for Gemini');
    assert.equal(hex.getAttribute('aria-label'), 'Hex color for Gemini');
    assert.equal(close.getAttribute('aria-label'), 'Close color editor for Gemini');

    await swatch.dispatch('click');
    assert.equal(editor.hidden, false);
    assert.equal(doc.activeElement, picker);
    picker.value = '#aabbcc';
    await picker.dispatch('input');
    assert.equal(picker.value, '#AABBCC');
    assert.equal(hex.value, '#AABBCC');
    assert.equal(controller.participantColors.gemini, '#AABBCC');
    assert.equal(swatch.style.getPropertyValue('--participant-color'), '#AABBCC');
    assert.equal(doc.getElementById('messages').children[0].style.getPropertyValue('--participant-color'), '#AABBCC');
    assert.equal(doc.getElementById('messages').children[1].style.getPropertyValue('--participant-color'), '#2F9E8F');

    const writesAfterValid = storage.setCalls.length;
    hex.value = '#12';
    await hex.dispatch('input');
    assert.equal(picker.value, '#AABBCC');
    assert.equal(controller.participantColors.gemini, '#AABBCC');
    assert.equal(storage.setCalls.length, writesAfterValid);
    hex.value = '#ZZZZZZ';
    await hex.dispatch('input');
    assert.equal(picker.value, '#AABBCC');
    assert.equal(storage.setCalls.length, writesAfterValid);

    hex.value = '#0a1b2c';
    await hex.dispatch('input');
    assert.equal(hex.value, '#0A1B2C');
    assert.equal(picker.value, '#0A1B2C');
    assert.equal(controller.participantColors.gemini, '#0A1B2C');

    await reset.dispatch('click');
    assert.equal(hex.value, '#4F8FEA');
    assert.equal(picker.value, '#4F8FEA');
    assert.equal(controller.participantColors.gemini, '#4F8FEA');
    assert.equal(Object.hasOwn(JSON.parse(storage.raw), 'gemini'), false);
    assert.equal(editor.hidden, false);

    await close.dispatch('click');
    assert.equal(editor.hidden, true);
    assert.equal(doc.activeElement, swatch);
    await swatch.dispatch('click');
    await doc.dispatch('keydown', { key: 'Escape' });
    assert.equal(editor.hidden, true);
    assert.equal(doc.activeElement, swatch);

    const roomRow = participantRow(doc, 'Room');
    await roomRow.querySelector('.participant-color-swatch').dispatch('click');
    const roomHex = roomRow.querySelector('.participant-color-hex');
    roomHex.value = '#102030';
    await roomHex.dispatch('input');
    assert.equal(doc.getElementById('messages').children[2].style.getPropertyValue('--participant-color'), '#102030');
    assert.equal(doc.getElementById('messages').children[0].style.getPropertyValue('--participant-color'), '#4F8FEA');
});


test('switching editors closes the previous editor and future messages use the live color', async () => {
    const doc = makeDirectoryDocument();
    const storage = new SyntheticStorage(null, { throwOnSet: true });
    const fetchStub = async url => url === '/api/participants'
        ? response(true, DIRECTORY)
        : response(true, []);
    const controller = setupApp(doc, fetchStub, storage);
    await settle();
    const geminiRow = participantRow(doc, 'Gemini');
    const heliosRow = participantRow(doc, 'Helios');
    const geminiSwatch = geminiRow.querySelector('.participant-color-swatch');
    const heliosSwatch = heliosRow.querySelector('.participant-color-swatch');
    const geminiEditor = geminiRow.querySelector('.participant-color-editor');
    await geminiSwatch.dispatch('click');
    const geminiHex = geminiEditor.querySelector('.participant-color-hex');
    geminiHex.value = '#334455';
    await geminiHex.dispatch('input');
    assert.equal(controller.participantColors.gemini, '#334455');
    assert.equal(storage.setCalls.length, 1);
    await heliosSwatch.dispatch('click');
    assert.equal(geminiEditor.hidden, true);
    assert.equal(heliosRow.querySelector('.participant-color-editor').hidden, false);

    displayMessages([syntheticMessage('gemini', 'Peter')], doc, controller.participantColors);
    assert.equal(
        doc.getElementById('messages').children[0].style.getPropertyValue('--participant-color'),
        '#334455'
    );
});


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


test('Trace v3 renders room-wide visibility and redaction without private labels', () => {
    const doc = makeDocument();
    renderTrace(minimalTrace({
        recorded_request: {
            is_redacted: false,
            request: {},
            local_context: {
                history_visibility: {
                    active_policy_version: 'room_shared_v1',
                    effective_from_room_sequence_no: 1,
                    projection_version: 'provider_history_v2'
                }
            }
        }
    }), doc);
    let text = allText(doc.getElementById('trace-body'));
    assert.match(text, /Room-wide history/);
    assert.match(text, /room_shared_v1/);
    assert.match(text, /provider_history_v2/);
    assert.doesNotMatch(text, /private/i);

    renderTrace(minimalTrace({
        recorded_request: {
            is_redacted: true,
            request: null,
            local_context: null
        }
    }), doc);
    text = allText(doc.getElementById('trace-body'));
    assert.match(text, /Room-wide history; request details redacted/);
});


test('Trace dialog uses a local inspection warning without a private label', () => {
    const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
    assert.match(html, /Local inspection:/);
    assert.doesNotMatch(html, /Private local inspection:/i);
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
    assert.equal(doc.getElementById('destination-button').textContent, 'Ask: Helios');
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
    assert.equal(doc.getElementById('destination-button').textContent, 'Ask: Room');
    input.value = '  exact room text  ';
    await input.dispatch('input');
    calls.length = 0;
    await doc.getElementById('message-form').dispatch('submit');
    const post = calls.find(call => call.options.method === 'POST');
    assert.deepEqual(JSON.parse(post.options.body), {
        message_text: '  exact room text  ',
        destination: { kind: 'room' }
    });
    assert.equal(doc.getElementById('destination-button').textContent, 'Ask: Room');
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


test('POST errors ignore attacker-controlled server messages and trust only positive accepted IDs', async () => {
    const hostile = '<img src=x onerror=alert("PRIVATE")>';
    for (const [payload, expected, accepted] of [
        [
            { error: 'gemini_provider_failure', message: hostile, turn_id: 7, peter_message_id: 8 },
            'Gemini could not respond. Your message was saved.',
            true
        ],
        [
            { error: 'future_attacker_code', message: hostile, turn_id: 7, peter_message_id: 8 },
            POST_ERROR_FALLBACK,
            true
        ],
        [
            { error: 'gemini_provider_failure', message: hostile, turn_id: '7', peter_message_id: 8 },
            'Gemini could not respond. Your message was saved.',
            false
        ]
    ]) {
        await assert.rejects(
            sendMessage('exact text', { kind: 'participant', participant_key: 'gemini' },
                async () => response(false, payload, 502)),
            error => error.message === expected
                && error.postAccepted === accepted
                && !error.message.includes('PRIVATE')
        );
    }
    await assert.rejects(
        sendMessage('exact text', { kind: 'participant', participant_key: 'gemini' },
            async () => { throw new Error(hostile); }),
        error => error.message === POST_ERROR_FALLBACK
            && error.postAccepted === false
            && error.code === null
    );
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
    assert.match(html, /style\.css\?v=direct-participant-addressing-v1/);
    assert.match(html, /app\.js\?v=direct-participant-addressing-v1/);
    assert.doesNotMatch(html, /gemini-participant-v1/);
    assert.match(css, /button:focus-visible/);
    assert.match(css, /input:focus-visible/);
    assert.match(css, /select:focus-visible/);
});


test('direct reply picker separates invocation from response routing and resets safely', async () => {
    const doc = makeDirectoryDocument();
    const calls = [];
    const fetchStub = async (url, options = {}) => {
        calls.push({ url, options });
        if (url === '/api/participants') return response(true, DIRECTORY);
        if (url === '/api/messages' && options.method === 'POST') {
            return response(true, { status: 'completed' });
        }
        if (url === '/api/messages') return response(true, []);
        throw new Error(`Unexpected URL ${url}`);
    };
    const controller = setupApp(doc, fetchStub);
    await settle();

    const control = doc.getElementById('response-destination-control');
    const select = doc.getElementById('response-destination-select');
    assert.equal(control.hidden, false);
    assert.equal(select.value, 'participant:peter');
    assert.deepEqual(select.children.map(option => option.textContent), ['Room', 'Gemini', 'Peter']);
    assert.equal(select.children.some(option => option.textContent === 'Helios'), false);

    select.value = 'participant:gemini';
    await select.dispatch('change');
    assert.equal(controller.getSelectedResponseDestination().participant_key, 'gemini');
    const input = doc.getElementById('message-input');
    input.value = 'Say hello.';
    await input.dispatch('input');
    calls.length = 0;
    await doc.getElementById('message-form').dispatch('submit');
    const post = calls.find(call => call.options.method === 'POST');
    assert.deepEqual(JSON.parse(post.options.body), {
        message_text: 'Say hello.',
        destination: { kind: 'participant', participant_key: 'helios' },
        response_destination: { kind: 'participant', participant_key: 'gemini' }
    });

    await participantRow(doc, 'Gemini').querySelector('.participant-entry').dispatch('click');
    assert.equal(doc.getElementById('destination-button').textContent, 'Ask: Gemini');
    assert.equal(select.value, 'participant:peter');
    assert.deepEqual(select.children.map(option => option.textContent), ['Room', 'Helios', 'Peter']);

    await participantRow(doc, 'Room').querySelector('.participant-entry').dispatch('click');
    assert.equal(control.hidden, true);
    assert.equal(controller.getSelectedResponseDestination(), null);
});


test('direct routing controls remain disabled for the full provider request', async () => {
    const doc = makeDirectoryDocument();
    let releasePost;
    const blocked = new Promise(resolve => { releasePost = resolve; });
    const fetchStub = async (url, options = {}) => {
        if (url === '/api/participants') return response(true, DIRECTORY);
        if (url === '/api/messages' && options.method === 'POST') {
            await blocked;
            return response(true, { status: 'completed' });
        }
        if (url === '/api/messages') return response(true, []);
        throw new Error(`Unexpected URL ${url}`);
    };
    setupApp(doc, fetchStub);
    await settle();
    const input = doc.getElementById('message-input');
    input.value = 'Wait for provider.';
    await input.dispatch('input');
    const submission = doc.getElementById('message-form').dispatch('submit');
    await settle();
    assert.equal(input.disabled, true);
    assert.equal(doc.getElementById('destination-button').disabled, true);
    assert.equal(doc.getElementById('response-destination-select').disabled, true);
    assert.match(allText(doc.getElementById('messages')), /Helios is responding to Peter/);
    releasePost();
    await submission;
    assert.equal(input.disabled, false);
    assert.equal(doc.getElementById('response-destination-select').disabled, false);
});


test('participant refresh resets a stale response target to Peter without selecting another AI', async () => {
    const doc = makeDirectoryDocument();
    let directory = DIRECTORY;
    const fetchStub = async url => {
        if (url === '/api/participants') return response(true, directory);
        if (url === '/api/messages') return response(true, []);
        throw new Error(`Unexpected URL ${url}`);
    };
    const controller = setupApp(doc, fetchStub);
    await settle();
    const select = doc.getElementById('response-destination-select');
    select.value = 'participant:gemini';
    await select.dispatch('change');
    directory = {
        ...DIRECTORY,
        destinations: DIRECTORY.destinations.filter(item => item.participant_key !== 'gemini')
    };
    await controller.loadDirectory();
    assert.equal(controller.getSelectedDestination().participant_key, 'helios');
    assert.equal(controller.getSelectedResponseDestination().participant_key, 'peter');
    assert.equal(select.children.some(option => option.textContent === 'Gemini'), false);
});


test('canonical direct routes render naturally while bubble color remains author-derived', () => {
    const doc = makeDocument();
    displayMessages([
        {
            ...syntheticMessage('helios', 'Gemini', 'Hello Gemini'),
            routing: {
                sender: { participant_key: 'helios', display_name: 'Helios' },
                destination: { kind: 'participant', participant_key: 'gemini', display_name: 'Gemini' }
            }
        },
        {
            ...syntheticMessage('gemini', 'Helios', 'Hello Helios'),
            routing: {
                sender: { participant_key: 'gemini', display_name: 'Gemini' },
                destination: { kind: 'participant', participant_key: 'helios', display_name: 'Helios' }
            }
        }
    ], doc);
    const messages = doc.getElementById('messages').children;
    assert.match(allText(messages[0]), /Helios -> Gemini/);
    assert.match(allText(messages[1]), /Gemini -> Helios/);
    assert.equal(messages[0].style.getPropertyValue('--participant-color'), '#D39A2C');
    assert.equal(messages[1].style.getPropertyValue('--participant-color'), '#4F8FEA');
});
