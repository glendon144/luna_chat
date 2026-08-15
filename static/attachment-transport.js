// Adapt the existing chat submit request to multipart when files are selected.
const lunaFetch = window.fetch;
window.fetch = async (url, options={}) => {
  if (url === '/chat' && options.body && options.headers?.['Content-Type'] === 'application/json') {
    const payload = JSON.parse(options.body);
    const files = getSelectedFilesForSend();
    const userMessage = document.querySelector('#messages .message.user:last-of-type');
    if (files.length && userMessage && !userMessage.querySelector('.attachment-note')) {
      const note = document.createElement('div');
      note.className = 'source-note attachment-note';
      note.textContent = `Attachments: ${files.map(file => file.name).join(', ')}`;
      userMessage.append(note);
    }
    const body = new FormData();
    Object.entries(payload).forEach(([key, value]) => body.append(key, value ?? ''));
    files.forEach(file => body.append('files', file, file.name));
    delete options.headers['Content-Type'];
    options.body = body;
    const response = await lunaFetch(url, options);
    if (response.ok) document.querySelector('#clear-files-button')?.click();
    return response;
  }
  return lunaFetch(url, options);
};
