"""
AI Request Queue with RPM Rate Limiting
========================================

Global asyncio-based request queue that rate-limits AI API calls.
All AI calls across all players go through this single queue to respect
the token provider's RPM (requests per minute) limit.

Players in the queue receive periodic status updates (position + ETA)
via WebSocket so they know they're waiting.

Design:
    The queue is a FIFO of asyncio.Event objects. When a caller calls
    `await queue.acquire(player_id)`, it appends an Event to the FIFO
    and waits. A background processor loop pops events one at a time,
    waiting for rate-limit tokens before signaling each event.
    The caller then runs its AI call inline (preserving the call stack)
    and calls `queue.release()` when done.
"""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field

from .config import settings

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Queue Item
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class QueueItem:
    """Represents a pending AI request slot in the queue."""
    player_id: str
    ready_event: asyncio.Event = field(default_factory=asyncio.Event)
    enqueue_time: float = field(default_factory=time.monotonic)
    label: str = ""
    cancelled: bool = False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Token Bucket Rate Limiter
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TokenBucketRateLimiter:
    """
    Token-bucket rate limiter for RPM control.
    Allows bursts up to `capacity`, refills at `rpm/60` tokens/sec.
    """

    def __init__(self, rpm: int):
        self.rpm = max(1, rpm)
        self.capacity = min(max(2, rpm // 6), 10)
        self.tokens = float(self.capacity)
        self.refill_rate = rpm / 60.0
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    def update_rpm(self, new_rpm: int):
        self.rpm = max(1, new_rpm)
        self.capacity = min(max(2, new_rpm // 6), 10)
        self.refill_rate = new_rpm / 60.0
        self.tokens = min(self.tokens, float(self.capacity))

    async def acquire(self):
        """Block until a token is available, then consume one."""
        while True:
            async with self._lock:
                self._refill()
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait = (1.0 - self.tokens) / self.refill_rate
            await asyncio.sleep(min(wait, 1.0))

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.last_refill = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)

    @property
    def estimated_wait_per_request(self) -> float:
        """Average seconds between requests at steady state."""
        return 60.0 / self.rpm


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Global AI Request Queue
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AIRequestQueue:
    """
    Global FIFO queue for AI requests with RPM rate limiting.

    Usage (context manager — guarantees release):

        async with request_queue.slot(player_id, label="streaming"):
            result = await ai_service.get_ai_response_stream(...)

    Or manual acquire/release:

        await request_queue.acquire(player_id, label="streaming")
        try:
            result = await ai_service.get_ai_response(...)
        finally:
            request_queue.release(player_id)
    """

    def __init__(self):
        rpm = getattr(settings, "AI_RPM", 40)
        self._rate_limiter = TokenBucketRateLimiter(rpm)
        self._queue: deque[QueueItem] = deque()
        self._processor_task: asyncio.Task | None = None
        self._notify_task: asyncio.Task | None = None
        self._shutdown = False
        self._new_item_event = asyncio.Event()

    def start(self):
        """Start the background processor + notifier. Call once at app startup."""
        if self._processor_task is None or self._processor_task.done():
            self._shutdown = False
            self._processor_task = asyncio.create_task(self._process_loop())
            self._notify_task = asyncio.create_task(self._notify_loop())
            logger.info(
                f"AI Request Queue started (RPM={self._rate_limiter.rpm}, "
                f"burst_capacity={self._rate_limiter.capacity})"
            )

    def stop(self):
        """Gracefully stop background tasks."""
        self._shutdown = True
        self._new_item_event.set()
        for task in (self._processor_task, self._notify_task):
            if task:
                task.cancel()
        self._processor_task = None
        self._notify_task = None

    # ── Public properties ──

    @property
    def queue_length(self) -> int:
        return len(self._queue)

    @property
    def rpm(self) -> int:
        return self._rate_limiter.rpm

    def get_player_position(self, player_id: str) -> int | None:
        """1-based position of first occurrence of player_id. None if not queued."""
        for i, item in enumerate(self._queue):
            if item.player_id == player_id and not item.cancelled:
                return i + 1
        return None

    # ── Acquire / Release ──

    async def acquire(self, player_id: str, label: str = "") -> QueueItem:
        """
        Enter the queue and block until it's this request's turn.
        Returns the QueueItem (for identification in release).
        """
        item = QueueItem(player_id=player_id, label=label)
        self._queue.append(item)
        self._new_item_event.set()

        pos = len(self._queue)
        logger.info(
            f"[Queue] +{player_id} pos={pos} label={label} "
            f"(queue_len={pos})"
        )

        # Send immediate status (only if actually waiting, i.e. pos > 1 or bucket empty)
        if pos > 1:
            await self._send_queue_status_to_player(player_id)

        # Wait until the processor signals us
        await item.ready_event.wait()
        
        # Notify frontend that we've left the queue (position=0)
        await self._send_queue_cleared(player_id)
        
        return item

    def release(self, player_id: str = "", item: QueueItem | None = None):
        """
        Signal that the AI call is done and the slot can be freed.
        This is a no-op bookkeeping call (rate limiting is in acquire).
        We also clean any cancelled items.
        """
        # Prune cancelled items from front of queue
        while self._queue and self._queue[0].cancelled:
            self._queue.popleft()

    def cancel(self, player_id: str):
        """Cancel all pending queue items for a player (e.g. on disconnect)."""
        for item in self._queue:
            if item.player_id == player_id and not item.ready_event.is_set():
                item.cancelled = True
                item.ready_event.set()  # Unblock the waiter

    class _Slot:
        """Async context manager for acquire/release."""
        def __init__(self, queue: "AIRequestQueue", player_id: str, label: str):
            self._queue = queue
            self._player_id = player_id
            self._label = label
            self._item: QueueItem | None = None

        async def __aenter__(self):
            self._item = await self._queue.acquire(self._player_id, self._label)
            if self._item.cancelled:
                raise asyncio.CancelledError("Queue item was cancelled")
            return self._item

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            self._queue.release(self._player_id, self._item)
            return False

    def slot(self, player_id: str, label: str = "") -> _Slot:
        """Return an async context manager that acquires and releases a queue slot."""
        return self._Slot(self, player_id, label)

    # ── Background processor ──

    async def _process_loop(self):
        """
        Pop items from the front of the queue, rate-limit, and signal them.
        Only ONE item is signaled at a time per rate-limit token.
        """
        while not self._shutdown:
            # Wait for items
            if not self._queue:
                self._new_item_event.clear()
                try:
                    await asyncio.wait_for(self._new_item_event.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue

            if self._shutdown:
                break

            # Skip cancelled items at the front
            while self._queue and self._queue[0].cancelled:
                discarded = self._queue.popleft()
                logger.debug(f"[Queue] Discarded cancelled item for {discarded.player_id}")

            if not self._queue:
                continue

            # Wait for rate-limit token
            await self._rate_limiter.acquire()

            # Re-check (might have been cancelled during the wait)
            while self._queue and self._queue[0].cancelled:
                self._queue.popleft()

            if not self._queue:
                continue

            # Signal the next item
            item = self._queue.popleft()
            wait_time = time.monotonic() - item.enqueue_time
            logger.info(
                f"[Queue] → {item.player_id} granted (waited {wait_time:.1f}s, "
                f"label={item.label}, remaining={len(self._queue)})"
            )
            item.ready_event.set()

            # Tiny yield to let the caller start executing
            await asyncio.sleep(0)

    # ── Notification loop ──

    async def _notify_loop(self):
        """Every 5 seconds, send queue_status to all waiting players."""
        while not self._shutdown:
            await asyncio.sleep(5.0)
            if self._queue:
                await self._broadcast_queue_status()

    async def _broadcast_queue_status(self):
        """Send position + ETA to every player still in the queue."""
        # Build a snapshot to avoid mutation during iteration
        snapshot = [(i, item) for i, item in enumerate(self._queue) if not item.cancelled]
        for i, item in snapshot:
            try:
                await self._send_queue_status_to_player(
                    item.player_id, position=i + 1
                )
            except Exception:
                pass

    async def _send_queue_status_to_player(
        self, player_id: str, position: int | None = None
    ):
        """Send a queue_status WebSocket message to a player."""
        from .websocket_manager import manager as websocket_manager

        if position is None:
            position = self.get_player_position(player_id)
        if position is None:
            return

        total = len(self._queue)
        eta = position * self._rate_limiter.estimated_wait_per_request

        message = {
            "type": "queue_status",
            "position": position,
            "total": total,
            "eta_seconds": round(eta, 1),
        }

        conn_info = websocket_manager.active_connections.get(player_id)
        if not conn_info:
            return

        try:
            await websocket_manager._send_compressed(conn_info["websocket"], message)
        except Exception:
            pass

    async def _send_queue_cleared(self, player_id: str):
        """Notify a player that they've left the queue (position=0)."""
        from .websocket_manager import manager as websocket_manager

        message = {
            "type": "queue_status",
            "position": 0,
            "total": len(self._queue),
            "eta_seconds": 0,
        }

        conn_info = websocket_manager.active_connections.get(player_id)
        if not conn_info:
            return

        try:
            await websocket_manager._send_compressed(conn_info["websocket"], message)
        except Exception:
            pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Module-level singleton
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

queue = AIRequestQueue()
