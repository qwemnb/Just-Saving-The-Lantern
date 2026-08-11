const API_BASE = '/api';

let currentParticipant = 'peter';

async function loadMessages() {
    try {
        const response = await fetch(`${API_BASE}/messages`);
        if (!response.ok) throw new Error('Failed to load messages');
        
        const messages = await response.json();
        displayMessages(messages);
    } catch (error) {
        console.error('Error loading messages:', error);
        showSystemMessage('Failed to load messages. Make sure the server is running.');
    }
}

function displayMessages(messages) {
    const messagesContainer = document.getElementById('messages');
    messagesContainer.innerHTML = '';
    
    if (messages.length === 0) {
        showSystemMessage('No messages yet. Start the conversation!');
        return;
    }
    
    messages.forEach(msg => {
        const messageDiv = document.createElement('div');
        messageDiv.className = `message ${msg.participant_key}`;
        
        const contentDiv = document.createElement('div');
        contentDiv.className = 'message-content';
        contentDiv.textContent = msg.message_text;
        
        const metaDiv = document.createElement('div');
        metaDiv.className = 'message-meta';
        const participantName = msg.participant_key === 'peter' ? 'Peter' : 'Helios';
        metaDiv.textContent = `${participantName} • ${formatTime(msg.created_at)}`;
        
        messageDiv.appendChild(contentDiv);
        messageDiv.appendChild(metaDiv);
        messagesContainer.appendChild(messageDiv);
    });
    
    scrollToBottom();
}

function showSystemMessage(text) {
    const messagesContainer = document.getElementById('messages');
    const messageDiv = document.createElement('div');
    messageDiv.className = 'message system';
    
    const contentDiv = document.createElement('div');
    contentDiv.className = 'message-content';
    contentDiv.textContent = text;
    
    messageDiv.appendChild(contentDiv);
    messagesContainer.appendChild(messageDiv);
    scrollToBottom();
}

function formatTime(timestamp) {
    const date = new Date(timestamp);
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function scrollToBottom() {
    const messagesContainer = document.getElementById('messages');
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
}

async function sendMessage(messageText) {
    try {
        const response = await fetch(`${API_BASE}/messages`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                message_text: messageText,
                participant_key: currentParticipant
            })
        });
        
        if (!response.ok) throw new Error('Failed to send message');
        
        const result = await response.json();
        await loadMessages(); // Reload all messages to show the new one
        return result;
    } catch (error) {
        console.error('Error sending message:', error);
        showSystemMessage('Failed to send message. Please try again.');
        throw error;
    }
}

document.addEventListener('DOMContentLoaded', () => {
    const form = document.getElementById('message-form');
    const input = document.getElementById('message-input');
    
    // Load initial messages
    loadMessages();
    
    // Handle form submission
    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        
        const messageText = input.value.trim();
        if (!messageText) return;
        
        const sendButton = form.querySelector('.send-button');
        sendButton.disabled = true;
        input.disabled = true;
        
        try {
            await sendMessage(messageText);
            input.value = '';
        } catch (error) {
            // Error already handled in sendMessage
        } finally {
            sendButton.disabled = false;
            input.disabled = false;
            input.focus();
        }
    });
});
