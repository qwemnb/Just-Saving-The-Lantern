const API_BASE = '/api';
const TRACE_USAGE = 'Usage: /trace or /trace <positive turn ID>';
const NO_MEMORY_RETRIEVAL = 'No inherited memory retrieval was recorded for this turn.';
const TRACE_PATTERN = /^\/trace(?:\s+([1-9][0-9]*))?\s*$/;

function classifyTraceCommand(text) {
    const stripped = text.trim();
    const match = TRACE_PATTERN.exec(stripped);
    if (match) {
        return match[1]
            ? { kind: 'turn', turnIdText: match[1] }
            : { kind: 'latest', turnIdText: null };
    }
    const firstToken = stripped ? stripped.split(/\s+/, 1)[0] : '';
    if (firstToken === '/trace') return { kind: 'malformed', turnIdText: null };
    return { kind: 'message', turnIdText: null };
}

async function loadMessages(fetchImpl = fetch, doc = document) {
    try {
        const response = await fetchImpl(`${API_BASE}/messages`);
        if (!response.ok) throw new Error('Failed to load messages');

        const messages = await response.json();
        displayMessages(messages, doc);
    } catch (error) {
        console.error('Error loading messages:', error);
        showSystemMessage('Failed to load messages. Make sure the server is running.', doc);
    }
}

function displayMessages(messages, doc = document) {
    const messagesContainer = doc.getElementById('messages');
    messagesContainer.replaceChildren();

    if (messages.length === 0) {
        showSystemMessage('No messages yet. Start the conversation!', doc);
        return;
    }

    messages.forEach(msg => {
        const messageDiv = doc.createElement('div');
        messageDiv.className = 'message canonical-message';

        const contentDiv = doc.createElement('div');
        contentDiv.className = 'message-content';
        contentDiv.textContent = msg.message_text;

        const metaDiv = doc.createElement('div');
        metaDiv.className = 'message-meta';
        metaDiv.textContent = `${String(msg.participant_key)} • ${formatTime(msg.created_at)}`;

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

async function sendMessage(messageText, fetchImpl = fetch) {
    const response = await fetchImpl(`${API_BASE}/messages`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({
            message_text: messageText
        })
    });

    let result = {};
    try {
        result = await response.json();
    } catch (error) {
        // Keep the stable fallback below if an intermediary returns non-JSON.
    }

    if (!response.ok) {
        const error = new Error(result.message || 'Failed to send message.');
        error.postAccepted = Boolean(result.turn_id && result.peter_message_id);
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

function setupApp(doc = document, fetchImpl = fetch) {
    const form = doc.getElementById('message-form');
    const input = doc.getElementById('message-input');
    const sendButton = form.querySelector('.send-button');
    const appShell = doc.getElementById('app-shell');
    const overlay = doc.getElementById('trace-overlay');
    const closeButton = doc.getElementById('trace-close');
    const traceStatus = doc.getElementById('trace-panel-status');
    const commandStatus = doc.getElementById('trace-command-status');

    function updateSendButtonState() {
        sendButton.disabled = input.disabled || input.value.trim().length === 0;
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
    closeButton.addEventListener('click', closeTracePanel);
    doc.addEventListener('keydown', event => {
        if (!overlay.hidden && event.key === 'Escape') {
            event.preventDefault();
            closeTracePanel();
        }
    });
    updateSendButtonState();
    loadMessages(fetchImpl, doc);

    form.addEventListener('submit', async (e) => {
        e.preventDefault();

        const messageText = input.value;
        if (!messageText.trim()) return;

        const command = classifyTraceCommand(messageText);
        if (command.kind === 'malformed') {
            commandStatus.textContent = TRACE_USAGE;
            return;
        }
        if (command.kind !== 'message') {
            await runTraceCommand(command);
            return;
        }

        commandStatus.textContent = '';
        sendButton.disabled = true;
        input.disabled = true;
        const pendingMessage = showSystemMessage('Helios is responding...', doc);

        try {
            await sendMessage(messageText, fetchImpl);
            input.value = '';
            await loadMessages(fetchImpl, doc);
        } catch (error) {
            console.error('Error sending message:', error);
            if (error.postAccepted) {
                input.value = '';
                await loadMessages(fetchImpl, doc);
            } else {
                pendingMessage.remove();
            }
            showSystemMessage(error.message || 'Failed to send message. Please try again.', doc);
        } finally {
            input.disabled = false;
            updateSendButtonState();
            input.focus();
        }
    });

    return { closeTracePanel, runTraceCommand };
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
        NO_MEMORY_RETRIEVAL
    };
}
