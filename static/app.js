const API_BASE = '/api';
const TRACE_USAGE = 'Usage: /trace or /trace <positive turn ID>';
const PARTICIPANTS_USAGE = 'Usage: /participants';
const NO_MEMORY_RETRIEVAL = 'No inherited memory retrieval was recorded for this turn.';
const TRACE_PATTERN = /^\/trace(?:\s+([1-9][0-9]*))?\s*$/;
const PARTICIPANTS_PATTERN = /^\/participants\s*$/;
const NETWORK_READ_ERROR = 'Cannot reach the Helios Room server.';
const HISTORY_READ_FALLBACK = 'Failed to load messages. Please refresh.';
const DIRECTORY_READ_FALLBACK = 'Participant directory unavailable.';
const POST_ERROR_FALLBACK = 'Failed to send message. Please try again.';
const POST_ERROR_LITERALS = Object.freeze({
    participant_destination_unavailable: 'The destination changed. Select a destination and try again.',
    missing_gemini_api_key: 'Gemini is not configured on this server.',
    missing_gemini_model: 'Gemini is not configured on this server.',
    gemini_provider_timeout: 'Gemini timed out. Your message was saved.',
    gemini_provider_failure: 'Gemini could not respond. Your message was saved.',
    gemini_provider_response_serialization_failed: "Gemini's response could not be recorded. Your message was saved.",
    gemini_provider_unusable_response: 'Gemini returned no usable response. Your message was saved.',
    turn_finalization_failed: 'The provider may have responded, but this turn requires manual reconciliation.'
});
const READ_ERROR_LITERALS = Object.freeze({
    message_history_invalid: 'The message history data is invalid.',
    message_history_unavailable: 'The message history is unavailable.',
    participant_directory_invalid: 'The participant directory data is invalid.',
    participant_directory_unavailable: 'The participant directory is unavailable.'
});
const PARTICIPANT_COLOR_STORAGE_KEY = 'helios-room.participant-colors.v1';
const PARTICIPANT_COLOR_PATTERN = /^#[0-9A-Fa-f]{6}$/;
const DEFAULT_PARTICIPANT_COLORS = Object.freeze({
    room: '#7C5CC4',
    gemini: '#4F8FEA',
    helios: '#D39A2C',
    peter: '#2F9E8F'
});
const NEUTRAL_PARTICIPANT_COLOR = '#D8D8E0';
const AUTHOR_COLOR_KEYS = Object.freeze({
    'room-system': 'room',
    gemini: 'gemini',
    helios: 'helios',
    peter: 'peter'
});

function normalizeParticipantColor(value) {
    return typeof value === 'string' && PARTICIPANT_COLOR_PATTERN.test(value)
        ? value.toUpperCase()
        : null;
}

function isPlainObject(value) {
    if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
    const prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
}

function loadParticipantColorOverrides(storage) {
    if (!storage) return {};
    let raw;
    try {
        if (typeof storage.getItem !== 'function') return {};
        raw = storage.getItem(PARTICIPANT_COLOR_STORAGE_KEY);
    } catch (_error) {
        return {};
    }
    if (raw === null) return {};
    let parsed;
    try {
        parsed = JSON.parse(raw);
    } catch (_error) {
        return {};
    }
    if (!isPlainObject(parsed)) return {};
    const overrides = {};
    Object.keys(DEFAULT_PARTICIPANT_COLORS).forEach(key => {
        if (!Object.prototype.hasOwnProperty.call(parsed, key)) return;
        const normalized = normalizeParticipantColor(parsed[key]);
        if (normalized !== null) overrides[key] = normalized;
    });
    return overrides;
}

function createParticipantColorPreferences(storage = null) {
    const overrides = loadParticipantColorOverrides(storage);
    const colors = { ...DEFAULT_PARTICIPANT_COLORS, ...overrides };

    function persist() {
        if (!storage) return;
        try {
            if (typeof storage.setItem !== 'function') return;
            storage.setItem(PARTICIPANT_COLOR_STORAGE_KEY, JSON.stringify(overrides));
        } catch (_error) {
            // A valid color remains active in memory when browser persistence is unavailable.
        }
    }

    function setColor(key, value) {
        if (!Object.prototype.hasOwnProperty.call(DEFAULT_PARTICIPANT_COLORS, key)) return false;
        const normalized = normalizeParticipantColor(value);
        if (normalized === null) return false;
        overrides[key] = normalized;
        colors[key] = normalized;
        persist();
        return true;
    }

    function resetColor(key) {
        if (!Object.prototype.hasOwnProperty.call(DEFAULT_PARTICIPANT_COLORS, key)) return false;
        delete overrides[key];
        colors[key] = DEFAULT_PARTICIPANT_COLORS[key];
        persist();
        return true;
    }

    return { colors, overrides, setColor, resetColor };
}

function browserLocalStorage() {
    try {
        return typeof window === 'undefined' ? null : window.localStorage;
    } catch (_error) {
        return null;
    }
}

function authorColorKey(authorKey) {
    return typeof authorKey === 'string'
        && Object.prototype.hasOwnProperty.call(AUTHOR_COLOR_KEYS, authorKey)
        ? AUTHOR_COLOR_KEYS[authorKey]
        : null;
}

function participantColorForAuthor(authorKey, colors = DEFAULT_PARTICIPANT_COLORS) {
    const key = authorColorKey(authorKey);
    if (key === null) return NEUTRAL_PARTICIPANT_COLOR;
    const normalized = normalizeParticipantColor(colors && colors[key]);
    return normalized === null ? DEFAULT_PARTICIPANT_COLORS[key] : normalized;
}

function setParticipantColorStyle(element, colorKey, color) {
    element.setAttribute('data-participant-color-key', colorKey || 'neutral');
    element.style.setProperty('--participant-color', color);
}

function classifyTraceCommand(text) {
    const stripped = text.trim();
    if (PARTICIPANTS_PATTERN.test(stripped)) {
        return { kind: 'participants', turnIdText: null };
    }
    const match = TRACE_PATTERN.exec(stripped);
    if (match) {
        return match[1]
            ? { kind: 'turn', turnIdText: match[1] }
            : { kind: 'latest', turnIdText: null };
    }
    const firstToken = stripped ? stripped.split(/\s+/, 1)[0] : '';
    if (firstToken === '/trace') return { kind: 'malformed', turnIdText: null };
    if (firstToken === '/participants') return { kind: 'malformed-participants', turnIdText: null };
    return { kind: 'message', turnIdText: null };
}

async function loadMessages(
    fetchImpl = fetch,
    doc = document,
    colors = DEFAULT_PARTICIPANT_COLORS
) {
    const status = doc.getElementById('history-read-status');
    let response;
    try {
        response = await fetchImpl(`${API_BASE}/messages`, { cache: 'no-store' });
    } catch (_error) {
        if (status) status.textContent = NETWORK_READ_ERROR;
        return false;
    }
    if (!response.ok) {
        if (status) {
            status.textContent = await localReadError(response, HISTORY_READ_FALLBACK);
        }
        return false;
    }
    let messages;
    try {
        messages = await response.json();
    } catch (_error) {
        if (status) status.textContent = HISTORY_READ_FALLBACK;
        return false;
    }
    if (!Array.isArray(messages)) {
        if (status) status.textContent = HISTORY_READ_FALLBACK;
        return false;
    }
    displayMessages(messages, doc, colors);
    if (status) status.textContent = '';
    return true;
}

async function localReadError(response, fallback) {
    try {
        const payload = await response.json();
        if (payload && typeof payload.error === 'string'
            && Object.prototype.hasOwnProperty.call(READ_ERROR_LITERALS, payload.error)) {
            return READ_ERROR_LITERALS[payload.error];
        }
    } catch (_error) {
        // Error bodies are deliberately ignored unless they match a local code.
    }
    return fallback;
}

function displayMessages(messages, doc = document, colors = DEFAULT_PARTICIPANT_COLORS) {
    const messagesContainer = doc.getElementById('messages');
    messagesContainer.replaceChildren();

    if (messages.length === 0) {
        showSystemMessage('No messages yet. Start the conversation!', doc);
        return;
    }

    messages.forEach(msg => {
        const messageDiv = doc.createElement('div');
        messageDiv.className = 'message canonical-message';
        const colorKey = authorColorKey(msg.participant_key);
        setParticipantColorStyle(
            messageDiv,
            colorKey,
            participantColorForAuthor(msg.participant_key, colors)
        );

        const contentDiv = doc.createElement('div');
        contentDiv.className = 'message-content';
        contentDiv.textContent = msg.message_text;

        const metaDiv = doc.createElement('div');
        metaDiv.className = 'message-meta';
        const routing = msg.routing || {};
        const sender = routing.sender || {};
        const destination = routing.destination || {};
        metaDiv.textContent = `${String(sender.display_name || msg.participant_key)} -> ${String(destination.display_name || '?')} • ${formatTime(msg.created_at)}`;

        messageDiv.appendChild(contentDiv);
        messageDiv.appendChild(metaDiv);
        messagesContainer.appendChild(messageDiv);
    });

    scrollToBottom(doc);
}

function showSystemMessage(text, doc = document) {
    const messagesContainer = doc.getElementById('messages');
    const messageDiv = doc.createElement('div');
    messageDiv.className = 'message system';
    setParticipantColorStyle(messageDiv, null, NEUTRAL_PARTICIPANT_COLOR);

    const contentDiv = doc.createElement('div');
    contentDiv.className = 'message-content';
    contentDiv.textContent = text;

    messageDiv.appendChild(contentDiv);
    messagesContainer.appendChild(messageDiv);
    scrollToBottom(doc);
    return messageDiv;
}

function formatTime(timestamp) {
    const date = new Date(timestamp);
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function scrollToBottom(doc = document) {
    const messagesContainer = doc.getElementById('messages');
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
}

async function sendMessage(messageText, destination, fetchImpl = fetch) {
    let response;
    try {
        response = await fetchImpl(`${API_BASE}/messages`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                message_text: messageText,
                destination: destination.kind === 'room'
                    ? { kind: 'room' }
                    : { kind: 'participant', participant_key: destination.participant_key }
            })
        });
    } catch (_error) {
        const error = new Error(POST_ERROR_FALLBACK);
        error.postAccepted = false;
        error.code = null;
        throw error;
    }

    let result = {};
    try {
        result = await response.json();
    } catch (error) {
        // Keep the stable fallback below if an intermediary returns non-JSON.
    }

    if (!response.ok) {
        const code = result && typeof result.error === 'string' ? result.error : null;
        const localMessage = code !== null
            && Object.prototype.hasOwnProperty.call(POST_ERROR_LITERALS, code)
            ? POST_ERROR_LITERALS[code]
            : POST_ERROR_FALLBACK;
        const error = new Error(localMessage);
        error.postAccepted = Number.isSafeInteger(result.turn_id) && result.turn_id > 0
            && Number.isSafeInteger(result.peter_message_id) && result.peter_message_id > 0;
        error.code = code;
        throw error;
    }

    return result;
}

function appendText(doc, parent, label, value) {
    const row = doc.createElement('div');
    row.className = 'trace-field';
    const heading = doc.createElement('strong');
    heading.textContent = `${label}: `;
    const text = doc.createElement('span');
    text.textContent = value === null || value === undefined ? 'null' : String(value);
    row.appendChild(heading);
    row.appendChild(text);
    parent.appendChild(row);
}

function appendPre(doc, parent, value) {
    const pre = doc.createElement('pre');
    pre.className = 'trace-pre';
    pre.textContent = typeof value === 'string'
        ? value
        : JSON.stringify(value, null, 2);
    parent.appendChild(pre);
    return pre;
}

function createSection(doc, body, title) {
    const section = doc.createElement('section');
    section.className = 'trace-section';
    const heading = doc.createElement('h3');
    heading.textContent = title;
    section.appendChild(heading);
    body.appendChild(section);
    return section;
}

function renderTrace(trace, doc = document) {
    const body = doc.getElementById('trace-body');
    body.replaceChildren();

    const turnSection = createSection(doc, body, 'Turn');
    const turn = trace.turn || {};
    appendText(doc, turnSection, 'ID', turn.id);
    appendText(doc, turnSection, 'Status', turn.status);
    appendText(doc, turnSection, 'Room', turn.room ? `${turn.room.room_key} — ${turn.room.name}` : null);
    appendText(doc, turnSection, 'Initiator', turn.initiated_by ? `${turn.initiated_by.participant_key} (${turn.initiated_by.participant_type})` : null);
    appendText(doc, turnSection, 'Created', turn.created_at);
    appendText(doc, turnSection, 'Completed', turn.completed_at);
    if (turn.completed_at) {
        const elapsed = new Date(turn.completed_at).getTime() - new Date(turn.created_at).getTime();
        appendText(doc, turnSection, 'Elapsed milliseconds', Number.isFinite(elapsed) ? elapsed : null);
    }

    const messagesSection = createSection(doc, body, 'Canonical messages');
    const messages = Array.isArray(trace.messages) ? trace.messages : [];
    if (!messages.length) appendText(doc, messagesSection, 'Messages', 'None recorded');
    messages.forEach(message => {
        const item = doc.createElement('article');
        item.className = 'trace-item';
        appendText(doc, item, 'Participant', message.participant_key);
        appendText(doc, item, 'Message ID', message.id);
        appendText(doc, item, 'Type', message.message_type);
        appendText(doc, item, 'Room sequence', message.room_sequence_no);
        appendText(doc, item, 'Turn sequence', message.turn_sequence_no);
        appendText(doc, item, 'Reply target', message.reply_to_id);
        appendText(doc, item, 'Reply target turn', message.reply_to_turn_id);
        appendText(doc, item, 'Reply outside selected turn', message.reply_to_outside_selected_turn);
        appendText(doc, item, 'Configuration ID', message.participant_config_id);
        appendText(doc, item, 'Created', message.created_at);
        const routing = message.routing || {};
        const routeSender = routing.sender || {};
        const routeDestination = routing.destination || {};
        appendText(doc, item, 'Routing mode', routing.routing_mode);
        appendText(doc, item, 'Route kind', routeDestination.kind);
        appendText(doc, item, 'Sender snapshot', routeSender.display_name);
        appendText(doc, item, 'Destination snapshot', routeDestination.display_name);
        appendText(doc, item, 'Exact message text', '');
        appendPre(doc, item, message.message_text);
        messagesSection.appendChild(item);
    });

    const configurationsSection = createSection(doc, body, 'Configurations');
    const configurations = Array.isArray(trace.configurations) ? trace.configurations : [];
    if (!configurations.length) appendText(doc, configurationsSection, 'Configurations', 'None referenced');
    configurations.forEach(configuration => {
        const item = doc.createElement('article');
        item.className = 'trace-item';
        appendText(doc, item, 'ID', configuration.id);
        appendText(doc, item, 'Participant', configuration.participant_key);
        appendText(doc, item, 'Provider', configuration.provider);
        appendText(doc, item, 'Model', configuration.model);
        appendText(doc, item, 'Label', configuration.config_label);
        appendText(doc, item, 'Created', configuration.created_at);
        appendText(doc, item, 'System instructions', '');
        appendPre(doc, item, configuration.system_instructions);
        appendText(doc, item, 'Settings', '');
        appendPre(doc, item, configuration.settings);
        appendText(doc, item, 'Tools', '');
        appendPre(doc, item, configuration.tools);
        appendText(doc, item, 'Omitted JSON pointers', '');
        appendPre(doc, item, configuration.omitted_json_pointers);
        configurationsSection.appendChild(item);
    });

    const requestSection = createSection(doc, body, 'Recorded provider request');
    if (trace.recorded_request === null || trace.recorded_request === undefined) {
        appendText(doc, requestSection, 'Request', 'None recorded');
    } else {
        appendPre(doc, requestSection, trace.recorded_request);
    }

    const visibilitySection = createSection(doc, body, 'Room-wide history');
    const recorded = trace.recorded_request;
    const visibility = recorded && recorded.local_context
        ? recorded.local_context.history_visibility
        : null;
    if (recorded && recorded.is_redacted) {
        appendText(doc, visibilitySection, 'Visibility', 'Room-wide history; request details redacted');
    } else if (visibility) {
        appendText(doc, visibilitySection, 'Visibility', 'Room-wide history');
        appendText(doc, visibilitySection, 'Policy version', visibility.active_policy_version);
        appendText(doc, visibilitySection, 'Effective sequence', visibility.effective_from_room_sequence_no);
        appendText(doc, visibilitySection, 'Projection version', visibility.projection_version);
    } else {
        appendText(doc, visibilitySection, 'Visibility', 'Room-wide history');
    }

    const outcomeSection = createSection(doc, body, 'Provider outcome');
    if (trace.provider_outcome === null || trace.provider_outcome === undefined) {
        appendText(doc, outcomeSection, 'Outcome', 'None recorded');
    } else {
        appendPre(doc, outcomeSection, trace.provider_outcome);
    }

    const eventsSection = createSection(doc, body, 'API event timeline');
    const events = Array.isArray(trace.api_events) ? trace.api_events : [];
    if (!events.length) appendText(doc, eventsSection, 'Events', 'None recorded');
    events.forEach(event => {
        const details = doc.createElement('details');
        details.className = 'trace-item';
        const summary = doc.createElement('summary');
        summary.textContent = `Sequence ${String(event.sequence_no)} — ${String(event.event_type)}`;
        details.appendChild(summary);
        appendText(doc, details, 'Created', event.created_at);
        appendText(doc, details, 'Related message', event.related_message_id);
        appendText(doc, details, 'Related message turn', event.related_message_turn_id);
        appendText(doc, details, 'Related outside selected turn', event.related_message_outside_selected_turn);
        appendText(doc, details, 'Redacted', event.is_redacted);
        appendText(doc, details, 'Redaction reason', event.redaction_reason);
        appendText(doc, details, 'Trace-safe event JSON', '');
        appendPre(doc, details, event);
        eventsSection.appendChild(details);
    });

    const inheritedSection = createSection(doc, body, 'Inherited memory');
    const inherited = trace.inherited_memory;
    const localContext = trace.recorded_request && trace.recorded_request.local_context;
    if (!inherited) {
        appendText(doc, inheritedSection, 'State', 'not_recorded');
        appendText(
            doc,
            inheritedSection,
            'Memory',
            localContext && Object.prototype.hasOwnProperty.call(localContext, 'memory_retrieval')
                ? 'A recorded local memory audit is available below.'
                : NO_MEMORY_RETRIEVAL
        );
    } else {
        appendText(doc, inheritedSection, 'State', inherited.state);
        appendText(doc, inheritedSection, 'Unavailable reason', inherited.unavailable_reason);
        if (inherited.retrieval !== null && inherited.retrieval !== undefined) {
            appendText(doc, inheritedSection, 'Recorded retrieval policy and selection', '');
            appendPre(doc, inheritedSection, inherited.retrieval);
        }
        if (inherited.context !== null && inherited.context !== undefined) {
            appendText(doc, inheritedSection, 'Exact supplied inherited context', '');
            appendPre(doc, inheritedSection, inherited.context);
        } else if (inherited.state === 'recorded') {
            appendText(doc, inheritedSection, 'Context', 'No inherited records were selected.');
        }
    }

    const memorySection = createSection(doc, body, 'Recorded local memory audit');
    if (localContext && Object.prototype.hasOwnProperty.call(localContext, 'memory_retrieval')) {
        appendPre(doc, memorySection, localContext.memory_retrieval);
    } else {
        appendText(doc, memorySection, 'Memory', NO_MEMORY_RETRIEVAL);
    }
}

function setupApp(doc = document, fetchImpl = fetch, storage = undefined) {
    const colorPreferences = createParticipantColorPreferences(
        arguments.length >= 3 ? storage : browserLocalStorage()
    );
    const form = doc.getElementById('message-form');
    const input = doc.getElementById('message-input');
    const sendButton = form.querySelector('.send-button');
    const appShell = doc.getElementById('app-shell');
    const overlay = doc.getElementById('trace-overlay');
    const closeButton = doc.getElementById('trace-close');
    const traceStatus = doc.getElementById('trace-panel-status');
    const commandStatus = doc.getElementById('trace-command-status');
    const destinationButton = doc.getElementById('destination-button');
    const picker = doc.getElementById('destination-picker');
    const search = doc.getElementById('destination-search');
    const options = doc.getElementById('destination-options');
    const participantList = doc.getElementById('participant-list');
    const participantPanel = doc.getElementById('participant-panel');
    const participantToggle = doc.getElementById('participants-toggle');
    const participantRefresh = doc.getElementById('participants-refresh');
    const participantStatus = doc.getElementById('participant-read-status');
    const hasDirectoryUi = Boolean(destinationButton && picker && search && options && participantList);
    let directory = null;
    let selectedDestination = hasDirectoryUi ? null : {
        kind: 'participant', participant_key: 'helios', primary_name: 'Helios'
    };
    let inFlight = false;
    let composing = false;
    let filteredDestinations = [];
    let activeOptionIndex = 0;
    let activeColorEditor = null;
    const colorSwatches = new Map();

    function destinationLabel(destination) {
        return destination.kind === 'room' ? destination.label : destination.primary_name;
    }

    function destinationColorKey(destination) {
        if (destination.kind === 'room') return 'room';
        return destination.kind === 'participant'
            && Object.prototype.hasOwnProperty.call(
                DEFAULT_PARTICIPANT_COLORS, destination.participant_key
            )
            ? destination.participant_key
            : null;
    }

    function closeColorEditor(returnFocus = true) {
        if (!activeColorEditor) return;
        const closing = activeColorEditor;
        activeColorEditor = null;
        closing.editor.hidden = true;
        closing.swatch.setAttribute('aria-expanded', 'false');
        if (returnFocus) closing.swatch.focus();
    }

    function updateParticipantColorPresentation(colorKey) {
        const color = colorPreferences.colors[colorKey];
        const swatch = colorSwatches.get(colorKey);
        if (swatch) setParticipantColorStyle(swatch, colorKey, color);
        const messagesContainer = doc.getElementById('messages');
        if (!messagesContainer) return;
        Array.from(messagesContainer.children).forEach(message => {
            if (message.getAttribute('data-participant-color-key') === colorKey) {
                message.style.setProperty('--participant-color', color);
            }
        });
    }

    function createColorEditor(colorKey, label, swatch) {
        const editor = doc.createElement('div');
        editor.id = `participant-color-editor-${colorKey}`;
        editor.className = 'participant-color-editor';
        editor.setAttribute('role', 'group');
        editor.setAttribute('aria-label', `Color editor for ${label}`);
        editor.hidden = true;

        const colorInput = doc.createElement('input');
        colorInput.type = 'color';
        colorInput.className = 'participant-color-picker';
        colorInput.value = colorPreferences.colors[colorKey];
        colorInput.setAttribute('aria-label', `Color picker for ${label}`);

        const hexInput = doc.createElement('input');
        hexInput.type = 'text';
        hexInput.className = 'participant-color-hex';
        hexInput.value = colorPreferences.colors[colorKey];
        hexInput.maxLength = 7;
        hexInput.autocomplete = 'off';
        hexInput.spellcheck = false;
        hexInput.setAttribute('aria-label', `Hex color for ${label}`);

        const resetButton = doc.createElement('button');
        resetButton.type = 'button';
        resetButton.className = 'participant-color-reset';
        resetButton.textContent = 'Reset';
        resetButton.setAttribute('aria-label', `Reset color for ${label}`);

        const closeButton = doc.createElement('button');
        closeButton.type = 'button';
        closeButton.className = 'participant-color-close';
        closeButton.textContent = 'Close';
        closeButton.setAttribute('aria-label', `Close color editor for ${label}`);

        colorInput.addEventListener('input', () => {
            const normalized = normalizeParticipantColor(colorInput.value);
            if (normalized === null) return;
            colorInput.value = normalized;
            hexInput.value = normalized;
            colorPreferences.setColor(colorKey, normalized);
            updateParticipantColorPresentation(colorKey);
        });
        hexInput.addEventListener('input', () => {
            const normalized = normalizeParticipantColor(hexInput.value);
            if (normalized === null) return;
            hexInput.value = normalized;
            colorInput.value = normalized;
            colorPreferences.setColor(colorKey, normalized);
            updateParticipantColorPresentation(colorKey);
        });
        resetButton.addEventListener('click', () => {
            colorPreferences.resetColor(colorKey);
            colorInput.value = colorPreferences.colors[colorKey];
            hexInput.value = colorPreferences.colors[colorKey];
            updateParticipantColorPresentation(colorKey);
        });
        closeButton.addEventListener('click', () => closeColorEditor(true));

        editor.appendChild(colorInput);
        editor.appendChild(hexInput);
        editor.appendChild(resetButton);
        editor.appendChild(closeButton);
        return editor;
    }

    function updateSendButtonState() {
        sendButton.disabled = inFlight || input.disabled || input.value.trim().length === 0 || !selectedDestination;
        if (destinationButton) destinationButton.disabled = inFlight || !directory;
    }

    function renderParticipantPanel() {
        if (!participantList) return;
        closeColorEditor(false);
        colorSwatches.clear();
        participantList.replaceChildren();
        if (!directory) {
            participantList.textContent = 'No verified participants.';
            return;
        }
        directory.destinations.forEach(destination => {
            const row = doc.createElement('div');
            row.className = 'participant-row';

            const item = doc.createElement('button');
            item.type = 'button';
            item.className = 'participant-entry';
            item.disabled = !destination.addressable;
            const primary = doc.createElement('strong');
            primary.textContent = destinationLabel(destination);
            item.appendChild(primary);
            if (destination.kind === 'participant' && destination.aliases.length > 1) {
                const aliases = doc.createElement('small');
                aliases.textContent = `Previously: ${destination.aliases.slice(1).join(', ')}`;
                item.appendChild(aliases);
            }
            if (!destination.addressable) {
                const unavailable = doc.createElement('small');
                unavailable.textContent = 'Not addressable';
                unavailable.className = 'availability-label';
                item.appendChild(unavailable);
            } else {
                item.addEventListener('click', () => selectDestination(destination));
            }
            row.appendChild(item);

            const colorKey = destinationColorKey(destination);
            if (colorKey !== null) {
                const label = destinationLabel(destination);
                const swatch = doc.createElement('button');
                swatch.type = 'button';
                swatch.className = 'participant-color-swatch';
                swatch.setAttribute('aria-label', `Choose color for ${label}`);
                swatch.setAttribute('aria-expanded', 'false');
                swatch.setAttribute('aria-controls', `participant-color-editor-${colorKey}`);
                setParticipantColorStyle(swatch, colorKey, colorPreferences.colors[colorKey]);
                colorSwatches.set(colorKey, swatch);

                const editor = createColorEditor(colorKey, label, swatch);
                swatch.addEventListener('click', event => {
                    if (typeof event.stopPropagation === 'function') event.stopPropagation();
                    closeColorEditor(false);
                    editor.hidden = false;
                    swatch.setAttribute('aria-expanded', 'true');
                    activeColorEditor = { editor, swatch };
                    editor.querySelector('.participant-color-picker').focus();
                });
                row.appendChild(swatch);
                row.appendChild(editor);
            }
            participantList.appendChild(row);
        });
    }

    function selectDestination(destination) {
        selectedDestination = destination;
        if (destinationButton) destinationButton.textContent = `To: ${destinationLabel(destination)}`;
        closePicker();
        updateSendButtonState();
        input.focus();
    }

    function renderPicker() {
        if (!options || !directory) return;
        const query = search.value.trim().toLocaleLowerCase();
        filteredDestinations = directory.destinations.filter(destination => {
            if (!destination.addressable) return false;
            const names = destination.kind === 'room'
                ? [destination.label]
                : destination.aliases;
            return !query || names.some(name => name.toLocaleLowerCase().includes(query));
        });
        activeOptionIndex = Math.min(activeOptionIndex, Math.max(0, filteredDestinations.length - 1));
        options.replaceChildren();
        filteredDestinations.forEach((destination, index) => {
            const option = doc.createElement('button');
            option.type = 'button';
            option.className = index === activeOptionIndex ? 'destination-option active' : 'destination-option';
            option.setAttribute('role', 'option');
            option.setAttribute('aria-selected', String(
                Boolean(selectedDestination
                    && selectedDestination.kind === destination.kind
                    && (destination.kind === 'room'
                        || selectedDestination.participant_key === destination.participant_key))
            ));
            option.textContent = destinationLabel(destination);
            option.addEventListener('click', () => selectDestination(destination));
            options.appendChild(option);
        });
    }

    function openPicker() {
        if (!picker || !directory || inFlight) return;
        picker.hidden = false;
        destinationButton.setAttribute('aria-expanded', 'true');
        search.value = '';
        activeOptionIndex = 0;
        renderPicker();
        search.focus();
    }

    function closePicker() {
        if (picker) picker.hidden = true;
        if (destinationButton) destinationButton.setAttribute('aria-expanded', 'false');
    }

    async function loadDirectory(autoSelect = true) {
        if (!hasDirectoryUi) return;
        directory = null;
        selectedDestination = null;
        destinationButton.textContent = 'Choose destination';
        closePicker();
        updateSendButtonState();
        renderParticipantPanel();
        let response;
        try {
            response = await fetchImpl(`${API_BASE}/participants`, { cache: 'no-store' });
        } catch (_error) {
            if (participantStatus) participantStatus.textContent = NETWORK_READ_ERROR;
            updateSendButtonState();
            return false;
        }
        if (!response.ok) {
            if (participantStatus) {
                participantStatus.textContent = await localReadError(
                    response, DIRECTORY_READ_FALLBACK
                );
            }
            updateSendButtonState();
            return false;
        }
        let payload;
        try {
            payload = await response.json();
        } catch (_error) {
            if (participantStatus) participantStatus.textContent = DIRECTORY_READ_FALLBACK;
            updateSendButtonState();
            return false;
        }
        if (payload.directory_version !== 1 || !Array.isArray(payload.destinations)) {
            directory = null;
            selectedDestination = null;
            if (participantStatus) participantStatus.textContent = DIRECTORY_READ_FALLBACK;
            updateSendButtonState();
            return false;
        }
        directory = payload;
        if (participantStatus) participantStatus.textContent = '';
        renderParticipantPanel();
        const helios = directory.destinations.find(item => item.kind === 'participant' && item.participant_key === 'helios' && item.addressable);
        if (helios && autoSelect) selectDestination(helios);
        updateSendButtonState();
        return true;
    }

    function openTracePanel(message) {
        overlay.hidden = false;
        appShell.inert = true;
        traceStatus.textContent = message;
        doc.getElementById('trace-body').replaceChildren();
        closeButton.focus();
    }

    function closeTracePanel() {
        overlay.hidden = true;
        appShell.inert = false;
        input.focus();
    }

    async function runTraceCommand(command) {
        input.value = '';
        updateSendButtonState();
        commandStatus.textContent = '';
        openTracePanel('Loading recorded trace…');
        const route = command.kind === 'latest'
            ? `${API_BASE}/trace/latest`
            : `${API_BASE}/trace/${command.turnIdText}`;
        try {
            const response = await fetchImpl(route, {
                method: 'GET',
                headers: { 'Accept': 'application/json' },
                cache: 'no-store'
            });
            let payload = {};
            try {
                payload = await response.json();
            } catch (error) {
                throw new Error('The trace endpoint returned an invalid response.');
            }
            if (!response.ok) throw new Error(payload.message || 'The recorded trace could not be loaded.');
            renderTrace(payload, doc);
            traceStatus.textContent = '';
        } catch (error) {
            traceStatus.textContent = error.message || 'The recorded trace could not be loaded.';
        }
    }

    input.addEventListener('input', updateSendButtonState);
    input.addEventListener('compositionstart', () => { composing = true; });
    input.addEventListener('compositionend', () => { composing = false; });
    input.addEventListener('keydown', event => {
        if (event.key === '[' && !composing && input.value.trim() === '') {
            event.preventDefault();
            openPicker();
        }
    });
    if (destinationButton) destinationButton.addEventListener('click', openPicker);
    if (search) {
        search.addEventListener('input', renderPicker);
        search.addEventListener('keydown', event => {
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                event.preventDefault();
                const direction = event.key === 'ArrowDown' ? 1 : -1;
                activeOptionIndex = Math.max(0, Math.min(filteredDestinations.length - 1, activeOptionIndex + direction));
                renderPicker();
            } else if (event.key === 'Enter' && filteredDestinations[activeOptionIndex]) {
                event.preventDefault();
                selectDestination(filteredDestinations[activeOptionIndex]);
            } else if (event.key === 'Escape') {
                event.preventDefault();
                closePicker();
                input.focus();
            }
        });
    }
    if (participantRefresh) participantRefresh.addEventListener('click', () => loadDirectory());
    if (participantToggle) participantToggle.addEventListener('click', () => {
        const open = participantPanel.classList.toggle('drawer-open');
        participantToggle.setAttribute('aria-expanded', String(open));
    });
    closeButton.addEventListener('click', closeTracePanel);
    doc.addEventListener('keydown', event => {
        if (activeColorEditor && event.key === 'Escape') {
            event.preventDefault();
            closeColorEditor(true);
            return;
        }
        if (!overlay.hidden && event.key === 'Escape') {
            event.preventDefault();
            closeTracePanel();
        }
    });
    updateSendButtonState();
    loadMessages(fetchImpl, doc, colorPreferences.colors);
    loadDirectory();

    form.addEventListener('submit', async (e) => {
        e.preventDefault();

        const messageText = input.value;
        if (!messageText.trim()) return;

        const command = classifyTraceCommand(messageText);
        if (command.kind === 'malformed') {
            commandStatus.textContent = TRACE_USAGE;
            return;
        }
        if (command.kind === 'malformed-participants') {
            commandStatus.textContent = PARTICIPANTS_USAGE;
            return;
        }
        if (command.kind === 'participants') {
            input.value = '';
            if (participantPanel) participantPanel.classList.add('drawer-open');
            if (participantToggle) participantToggle.setAttribute('aria-expanded', 'true');
            await loadDirectory();
            return;
        }
        if (command.kind !== 'message') {
            await runTraceCommand(command);
            return;
        }

        if (!selectedDestination) return;

        commandStatus.textContent = '';
        inFlight = true;
        sendButton.disabled = true;
        input.disabled = true;
        if (destinationButton) destinationButton.disabled = true;
        const pendingText = selectedDestination.kind === 'room'
            ? 'Saving message to the Room...'
            : `${destinationLabel(selectedDestination)} is responding...`;
        const pendingMessage = showSystemMessage(pendingText, doc);

        try {
            await sendMessage(messageText, selectedDestination, fetchImpl);
            input.value = '';
            await loadMessages(fetchImpl, doc, colorPreferences.colors);
        } catch (error) {
            console.error('Error sending message:', error);
            if (error.postAccepted) {
                input.value = '';
                await loadMessages(fetchImpl, doc, colorPreferences.colors);
            } else {
                pendingMessage.remove();
            }
            if (error.code === 'participant_destination_unavailable') {
                await loadDirectory(false);
                commandStatus.textContent = 'The destination changed. Select a destination and try again.';
            }
            showSystemMessage(error.message || 'Failed to send message. Please try again.', doc);
        } finally {
            inFlight = false;
            input.disabled = false;
            updateSendButtonState();
            input.focus();
        }
    });

    return {
        closeTracePanel,
        runTraceCommand,
        loadDirectory,
        openPicker,
        participantColors: colorPreferences.colors
    };
}

if (typeof document !== 'undefined') {
    document.addEventListener('DOMContentLoaded', () => setupApp(document, fetch));
}

if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        classifyTraceCommand,
        renderTrace,
        setupApp,
        TRACE_USAGE,
        PARTICIPANTS_USAGE,
        NO_MEMORY_RETRIEVAL,
        loadMessages,
        localReadError,
        READ_ERROR_LITERALS,
        NETWORK_READ_ERROR,
        HISTORY_READ_FALLBACK,
        DIRECTORY_READ_FALLBACK,
        POST_ERROR_LITERALS,
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
    };
}
