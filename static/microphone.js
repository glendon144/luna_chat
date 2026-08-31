(() => {
  const button = document.querySelector('#microphone-button');
  const status = document.querySelector('#microphone-status');
  const input = document.querySelector('#message-input');
  if (!button || !status || !input) return;

  const MAX_RECORDING_MS = 120000;
  const MIME_CANDIDATES = [
    'audio/webm;codecs=opus',
    'audio/webm',
    'audio/ogg;codecs=opus',
    'audio/mp4',
  ];

  let recorder = null;
  let stream = null;
  let chunks = [];
  let stopTimer = null;
  let transcribing = false;

  function setStatus(message, isError = false) {
    status.textContent = message;
    status.classList.toggle('error', isError);
  }

  function cleanupStream() {
    if (stopTimer) {
      clearTimeout(stopTimer);
      stopTimer = null;
    }
    if (stream) {
      for (const track of stream.getTracks()) track.stop();
      stream = null;
    }
  }

  function resetButton() {
    button.disabled = false;
    button.classList.remove('recording');
    button.setAttribute('aria-pressed', 'false');
    button.textContent = 'Microphone';
  }

  function supportedMimeType() {
    if (!window.MediaRecorder?.isTypeSupported) return '';
    return MIME_CANDIDATES.find(type => MediaRecorder.isTypeSupported(type)) || '';
  }

  function filenameForMimeType(type) {
    if (type.includes('ogg')) return 'recording.ogg';
    if (type.includes('mp4')) return 'recording.m4a';
    return 'recording.webm';
  }

  async function transcribe(blob) {
    if (!blob || blob.size === 0) throw new Error('The recording was empty.');
    transcribing = true;
    button.disabled = true;
    button.textContent = 'Transcribing…';
    setStatus('Converting speech to editable text…');

    const data = new FormData();
    data.append('audio', blob, filenameForMimeType(blob.type || 'audio/webm'));
    const response = await fetch('/api/transcribe', { method: 'POST', body: data });
    let payload = {};
    try { payload = await response.json(); } catch {}
    if (!response.ok) throw new Error(payload.error || `Transcription failed (HTTP ${response.status}).`);
    const text = String(payload.text || '').trim();
    if (!text) throw new Error('No speech was detected.');

    const before = input.value;
    input.value = before.trim() ? `${before.replace(/\s+$/, '')}\n${text}` : text;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
    setStatus('Transcript added to the message box. Edit it or press Send.');
  }

  async function startRecording() {
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      setStatus('Microphone capture is unavailable here. Use HTTPS or localhost in a browser with recording support.', true);
      return;
    }

    try {
      setStatus('Requesting microphone permission…');
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mimeType = supportedMimeType();
      recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
      chunks = [];

      recorder.addEventListener('dataavailable', event => {
        if (event.data?.size) chunks.push(event.data);
      });

      recorder.addEventListener('stop', async () => {
        const type = recorder?.mimeType || mimeType || 'audio/webm';
        const blob = new Blob(chunks, { type });
        chunks = [];
        cleanupStream();
        recorder = null;
        try {
          await transcribe(blob);
        } catch (error) {
          setStatus(error?.message || 'Transcription failed.', true);
        } finally {
          transcribing = false;
          resetButton();
        }
      }, { once: true });

      recorder.addEventListener('error', event => {
        cleanupStream();
        recorder = null;
        transcribing = false;
        resetButton();
        setStatus(event.error?.message || 'The browser could not record audio.', true);
      }, { once: true });

      recorder.start(250);
      button.classList.add('recording');
      button.setAttribute('aria-pressed', 'true');
      button.textContent = 'Stop Recording';
      setStatus('Recording… press Stop Recording when you finish.');
      stopTimer = setTimeout(() => {
        if (recorder?.state === 'recording') {
          setStatus('Two-minute recording limit reached; transcribing now.');
          recorder.stop();
        }
      }, MAX_RECORDING_MS);
    } catch (error) {
      cleanupStream();
      recorder = null;
      resetButton();
      const denied = error?.name === 'NotAllowedError' || error?.name === 'SecurityError';
      setStatus(
        denied
          ? 'Microphone permission was denied, or this page is not allowed to use it. HTTPS or localhost may be required.'
          : (error?.message || 'Unable to start the microphone.'),
        true,
      );
    }
  }

  button.addEventListener('click', () => {
    if (transcribing) return;
    if (recorder?.state === 'recording') {
      button.disabled = true;
      button.textContent = 'Stopping…';
      recorder.stop();
      return;
    }
    startRecording();
  });

  window.addEventListener('pagehide', () => {
    if (recorder?.state === 'recording') {
      try { recorder.stop(); } catch {}
    }
    cleanupStream();
  });

  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    button.disabled = true;
    button.title = 'Microphone capture requires browser MediaRecorder support and usually HTTPS or localhost.';
    setStatus('Microphone unavailable in this browser/context. Typing still works normally.');
  }
})();
