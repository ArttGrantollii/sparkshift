// One request at a time. Killing the worker interrupts even synchronous
// Python: a cancel message alone cannot run while that worker is blocked.
export const STARTUP_TIMEOUT_MS = 120_000;
export const CONVERSION_TIMEOUT_MS = 10_000;

function failure(name) {
  return Object.assign(new Error("The browser worker request failed."), { name });
}

export class ConversionWorker {
  constructor(onIdleFailure) {
    this.worker = new Worker(new URL("./worker.js", import.meta.url), { type: "module" });
    this.pending = null;
    this.nextId = 0;
    this.closed = false;
    this.worker.onmessage = ({ data }) => {
      if (!this.pending || data.id !== this.pending.id) return;
      const { resolve, reject, timer } = this.pending;
      this.pending = null;
      clearTimeout(timer);
      if (data.error) reject(failure("ConversionError"));
      else resolve(data.result);
    };
    const workerFailed = () => {
      const hadRequest = this.pending !== null;
      this.close(failure("WorkerError"));
      if (!hadRequest) onIdleFailure();
    };
    this.worker.onerror = (event) => {
      event.preventDefault();
      workerFailed();
    };
    this.worker.onmessageerror = workerFailed;
  }

  request(type, payload, timeout) {
    if (this.closed) return Promise.reject(failure("WorkerError"));
    if (this.pending) return Promise.reject(failure("BusyError"));
    return new Promise((resolve, reject) => {
      const id = ++this.nextId;
      const timer = setTimeout(() => this.close(failure("TimeoutError")), timeout);
      this.pending = { id, resolve, reject, timer };
      try {
        this.worker.postMessage({ id, type, ...payload });
      } catch {
        this.close(failure("WorkerError"));
      }
    });
  }

  close(error = failure("AbortError")) {
    if (this.closed) return;
    this.closed = true;
    this.worker.terminate();
    if (this.pending) {
      const { reject, timer } = this.pending;
      this.pending = null;
      clearTimeout(timer);
      reject(error);
    }
  }
}
