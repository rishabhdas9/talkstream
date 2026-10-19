export class FeedbackCueLifecycle {
  private generation = 0;
  private active: { turnId: string; cueId: string; generation: number } | null = null;

  begin(turnId: string, cueId: string): number {
    this.generation += 1;
    this.active = { turnId, cueId, generation: this.generation };
    return this.generation;
  }

  canStart(turnId: string, cueId: string, generation: number): boolean {
    return (
      this.active?.turnId === turnId &&
      this.active.cueId === cueId &&
      this.active.generation === generation
    );
  }

  cancel(turnId: string, cueId: string): boolean {
    if (this.active?.turnId !== turnId || this.active.cueId !== cueId) return false;
    this.invalidate();
    return true;
  }

  invalidate(): void {
    this.generation += 1;
    this.active = null;
  }
}
