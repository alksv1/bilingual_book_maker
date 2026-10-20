/**
 * Server-Sent Events (SSE) log stream subscriber.
 */

export class LogStreamer {
  constructor(taskId, { onMessage, onError, onOpen } = {}) {
    this.taskId = taskId;
    this.onMessage = onMessage || (() => {});
    this.onError = onError || (() => {});
    this.onOpen = onOpen || (() => {});
    this.eventSource = null;
    this.isConnected = false;
  }

  connect() {
    this.close();

    const url = `/api/tasks/${encodeURIComponent(this.taskId)}/logs/stream`;
    this.eventSource = new EventSource(url);

    this.eventSource.onopen = (e) => {
      this.isConnected = true;
      this.onOpen(e);
    };

    this.eventSource.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        this.onMessage(data);
      } catch (err) {
        // Fallback for plain text lines
        this.onMessage({ time: new Date().toLocaleTimeString(), line: e.data });
      }
    };

    this.eventSource.onerror = (e) => {
      this.onError(e);
    };
  }

  close() {
    if (this.eventSource) {
      this.eventSource.close();
      this.eventSource = null;
      this.isConnected = false;
    }
  }
}
