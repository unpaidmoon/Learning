/** Original cursor bodies, probe-only shell: no socket or command replay. */
type StreamEvent={type:string;turn_id?:string;seq?:number;[key:string]:unknown};
export class ReplayCursorProbe {
 turnId:string|null=null;lastSeq=0;buffered=new Map<number,StreamEvent>();terminalObserved=false;
 pending:Array<{requiresAck:boolean;acknowledgedAfter:number}>=[];
 events:StreamEvent[]=[];diagnostics:string[]=[];probeDelays:number[]=[];replayRequests=0;clears=0;
 options={maxBufferedGap:32,replayProbeDelayMs:1000,onEvent:(e:StreamEvent)=>this.events.push(e),onDiagnostic:(s:string)=>this.diagnostics.push(s)};
 clearReplayProbe(){this.clears++;} scheduleReplayProbe(delay=1000){this.probeDelays.push(delay);} requestReplay(){this.replayRequests++;}
 feed(event:StreamEvent){this.acceptStreamEvent(event);}
 setResumeCursor(turnId: string | null, afterSeq: number): void {
    if (!Number.isInteger(afterSeq) || afterSeq < 0)
      throw new TypeError("afterSeq is invalid");
    const nextTurnId = turnId?.trim() || null;
    if (nextTurnId !== this.turnId) {
      this.clearReplayProbe();
      this.turnId = nextTurnId;
      this.lastSeq = afterSeq;
      this.buffered.clear();
      this.terminalObserved = false;
      return;
    }
    // React state can trail the socket by one render. Never rewind a live
    // transport cursor from that stale snapshot or already-consumed events
    // (including DONE) can be replayed into a new UI state.
    if (afterSeq <= this.lastSeq) return;
    this.lastSeq = afterSeq;
    for (const seq of this.buffered.keys()) {
      if (seq <= afterSeq) this.buffered.delete(seq);
    }
  }
private acceptStreamEvent(event: StreamEvent): void {
    const eventTurnId = event.turn_id?.trim() || null;
    if (eventTurnId && eventTurnId !== this.turnId) {
      this.clearReplayProbe();
      this.turnId = eventTurnId;
      this.lastSeq = 0;
      this.buffered.clear();
      this.terminalObserved = false;
    }
    const seq = event.seq ?? 0;
    if (seq <= this.lastSeq) return;
    const gap = seq - this.lastSeq;
    if (gap > 1) {
      if (gap <= this.options.maxBufferedGap) {
        this.buffered.set(seq, event);
        // WebSockets preserve frame order, so a bounded gap is normally a
        // dropped/rejected frame rather than harmless reordering. Give an
        // in-flight predecessor one tick to arrive, then replay the durable
        // suffix instead of buffering DONE forever.
        this.scheduleReplayProbe(
          Math.min(250, this.options.replayProbeDelayMs),
        );
      } else if (this.turnId) {
        this.options.onDiagnostic?.(
          `turn event gap exceeded buffer; after_seq=${this.lastSeq}`,
        );
        this.requestReplay();
      }
      return;
    }

    this.emitInOrder(event);
    let next = this.buffered.get(this.lastSeq + 1);
    while (next) {
      this.buffered.delete(this.lastSeq + 1);
      this.emitInOrder(next);
      next = this.buffered.get(this.lastSeq + 1);
    }
  }
private emitInOrder(event: StreamEvent): void {
    this.lastSeq = event.seq ?? this.lastSeq;
    this.pending = this.pending.filter(
      (item) => item.requiresAck || this.lastSeq <= item.acknowledgedAfter,
    );
    if (event.type === "done") {
      this.terminalObserved = true;
      this.clearReplayProbe();
    }
    this.options.onEvent(event);
    if (event.type === "done") return;
    if (!this.terminalObserved) this.scheduleReplayProbe();
  }
}
