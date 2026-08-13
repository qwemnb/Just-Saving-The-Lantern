const API_BASE = '/api';
const TRACE_USAGE = 'Usage: /trace or /trace <positive turn ID>';
const PARTICIPANTS_USAGE = 'Usage: /participants';
const NO_MEMORY_RETRIEVAL = 'No inherited memory retrieval was recorded for this turn.';
const TRACE_PATTERN = /^\/trace(?:\s+([1-9][0-9]*))?\s*$/;
const PARTICIPANTS_PATTERN = /^\/participants\s*$/;
const NETWORK_READ_ERROR = 'Cannot reach the Helios Room server.';
const HISTORY_READ_FALLBACK = 'Failed to load messages. Please refresh.';
const DIRECTORY_READ_FALLBACK = 'Participant directory unavailable.';
const READ_ERROR_LITERALS = Object.freeze({
    message_history_invalid: 'The message history data is invalid.',
    message_history_unavailable: 'The message history is unavailable.',
    participant_directory_invalid: 'The participant directory data is invalid.',
    participant_directory_unavailable: 'The participant directory is unavailable.'
});

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

async function loadMessages(fetchImpl = fetch, doc = document) {
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
    displayMessages(messages, doc);
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
    const response = await fetchImpl(`${API_BASE}/messages`, {
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

    let result = {};
    try {
        result = await response.json();
    } catch (error) {
        // Keep the stable fallback below if an intermediary returns non-JSON.
    }

    if (!response.ok) {
        const error = new Error(result.message || 'Failed to send message.');
        error.postAccepted = Boolean(result.turn_id && result.peter_message_id);
        error.code = result.error;
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

    function destinationLabel(destination) {
        return destination.kind === 'room' ? destination.label : destination.primary_name;
    }

    function updateSendButtonState() {
        sendButton.disabled = inFlight || input.disabled || input.value.trim().length === 0 || !selectedDestination;
        if (destinationButton) destinationButton.disabled = inFlight || !directory;
    }

    function renderParticipantPanel() {
        if (!participantList) return;
        participantList.replaceChildren();
        if (!directory) {
            participantList.textContent = 'No verified participants.';
            return;
        }
        directory.destinations.forEach(destination => {
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
            participantList.appendChild(item);
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
        if (!overlay.hidden && event.key === 'Escape') {
            event.preventDefault();
            closeTracePanel();
        }
    });
    updateSendButtonState();
    loadMessages(fetchImpl, doc);
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
            await loadMessages(fetchImpl, doc);
        } catch (error) {
            console.error('Error sending message:', error);
            if (error.postAccepted) {
                input.value = '';
                await loadMessages(fetchImpl, doc);
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

    return { closeTracePanel, runTraceCommand, loadDirectory, openPicker };
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
        DIRECTORY_READ_FALLBACK
    };
}
