// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// Per-service log capture: an in-memory ring buffer for the UI plus a rolling
// file sink, so a crash is still readable after the fact. The app's own
// monitoring/logs tree is untouched.

const fs = require('fs');
const path = require('path');
const { EventEmitter } = require('events');

const MAX_LINES = 5000;
const MAX_FILES_PER_SERVICE = 5;
const FLUSH_INTERVAL_MS = 100;
const MAX_LINE_CHARS = 4000;

// Every child service is polled for liveness once or twice a second, which
// drowns the log view in uvicorn access lines. The main app already filters its
// own; this covers the ones teed in from children. Only successful probes are
// dropped, so a failing health check is still visible.
const PROBE_NOISE = /"(?:GET|HEAD) [^"]*\/(?:health|heartbeat|metrics|ping)[^"]*" [23]\d\d/;

let nextSeq = 1;

class LogStore extends EventEmitter {
  constructor(dir) {
    super();
    this.dir = dir;
    this.buffers = new Map(); // id -> line[]
    this.sinks = new Map(); // id -> { stream, file }
    this.pending = new Map(); // id -> line[]
    this.partials = new Map(); // `${id} ${stream}` -> string
    this.flushTimer = null;
    // Optional (text) => { id, text } | null, used to attribute a child's teed
    // output to the child rather than the process that piped it.
    this.route = () => null;
    // Optional (id, text) => boolean: true takes the line out of the log, for
    // lines that carry state rather than text, such as download progress.
    this.tap = () => false;
  }

  setRoute(route) {
    this.route = typeof route === 'function' ? route : () => null;
  }

  setTap(tap) {
    this.tap = typeof tap === 'function' ? tap : () => false;
  }

  openSink(id) {
    this.closeSink(id);
    try {
      fs.mkdirSync(this.dir, { recursive: true });
      this.prune(id);
      const stamp = new Date().toISOString().replace(/[:.]/g, '-');
      const file = path.join(this.dir, `${id}-${stamp}.log`);
      this.sinks.set(id, { stream: fs.createWriteStream(file, { flags: 'a' }), file });
      return file;
    } catch {
      // Log capture is best-effort; never block a service start on it.
      return null;
    }
  }

  closeSink(id) {
    const sink = this.sinks.get(id);
    if (!sink) return;
    sink.stream.end();
    this.sinks.delete(id);
  }

  prune(id) {
    const files = fs
      .readdirSync(this.dir)
      .filter((name) => name.startsWith(`${id}-`) && name.endsWith('.log'))
      .sort();
    for (const name of files.slice(0, Math.max(0, files.length - (MAX_FILES_PER_SERVICE - 1)))) {
      fs.rmSync(path.join(this.dir, name), { force: true });
    }
  }

  logFile(id) {
    return this.sinks.get(id)?.file ?? null;
  }

  // `chunk` is raw stdout/stderr; partial trailing lines are held until the
  // rest arrives so progress output is not split mid-line. Per stream, so a
  // stderr line cannot land in the middle of a stdout one.
  write(id, chunk, stream = 'stdout') {
    const key = `${id} ${stream}`;
    const text = (this.partials.get(key) || '') + chunk.toString('utf8').replace(/\r(?!\n)/g, '\n');
    const parts = text.split(/\r?\n/);
    this.partials.set(key, parts.pop() ?? '');
    for (const part of parts) {
      const routed = this.route(part);
      const target = routed && routed.id !== id ? routed : { id, text: part };
      // After routing, so a child's progress is the child's.
      if (this.tap(target.id, target.text)) continue;
      // The file sink stays with the source so one log file holds the whole tree.
      this.push(target.id, target.text, stream, id);
    }
  }

  // Manager-generated status lines (start/stop/exit), not child output.
  note(id, text) {
    this.push(id, text, 'manager', id);
    this.flush();
  }

  push(id, text, stream, sinkId = id) {
    const trimmed = text.length > MAX_LINE_CHARS ? `${text.slice(0, MAX_LINE_CHARS)}…` : text;
    const ts = Date.now();

    const sink = this.sinks.get(sinkId);
    if (sink?.stream.writable) {
      sink.stream.write(`[${new Date(ts).toISOString()}] [${stream}] ${trimmed}\n`);
    }

    // Kept on disk for post-mortem, hidden from the UI.
    if (PROBE_NOISE.test(trimmed)) return;

    const line = { seq: nextSeq++, ts, stream, text: trimmed };

    const buffer = this.buffers.get(id) || [];
    buffer.push(line);
    if (buffer.length > MAX_LINES) buffer.splice(0, buffer.length - MAX_LINES);
    this.buffers.set(id, buffer);

    const pending = this.pending.get(id) || [];
    pending.push(line);
    this.pending.set(id, pending);
    this.scheduleFlush();
  }

  scheduleFlush() {
    if (this.flushTimer) return;
    this.flushTimer = setTimeout(() => this.flush(), FLUSH_INTERVAL_MS);
  }

  // Coalesced so a chatty child (pip, model download) cannot flood IPC.
  flush() {
    clearTimeout(this.flushTimer);
    this.flushTimer = null;
    for (const [id, lines] of this.pending) {
      if (lines.length) this.emit('append', { id, lines });
    }
    this.pending.clear();
  }

  read(id, { limit = 1000, sinceSeq = 0 } = {}) {
    const buffer = this.buffers.get(id) || [];
    const filtered = sinceSeq ? buffer.filter((line) => line.seq > sinceSeq) : buffer;
    return filtered.slice(-limit);
  }

  clear(id) {
    this.buffers.delete(id);
    this.pending.delete(id);
  }

  dispose() {
    clearTimeout(this.flushTimer);
    this.flushTimer = null;
    for (const id of [...this.sinks.keys()]) this.closeSink(id);
  }

  /**
   * dispose(), resolving once every log file is written out. File streams
   * write asynchronously, so a process that exits straight after dispose()
   * can lose the end of its log, which is the part that explains a failure.
   */
  async disposeAndFlush() {
    const streams = [...this.sinks.values()].map((sink) => sink.stream);
    this.dispose();
    await Promise.all(
      streams.map(
        (stream) =>
          new Promise((resolve) => {
            if (stream.closed || stream.destroyed) return resolve();
            stream.once('close', resolve);
            stream.once('error', resolve);
          })
      )
    );
  }
}

module.exports = { LogStore, MAX_LINES };
