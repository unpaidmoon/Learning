"""Bounded recovery of this application's failed Host, never prompt retries."""

import asyncio


class ExecutionSupervisor:
    def __init__(self, app, *, restart_limit=2, delays=(1, 4)):
        self.app = app
        self.restart_limit, self.delays = restart_limit, delays
        self.used = 0
        self.state = "disabled"
        self.error_code = None
        self.closed = False
        self.monitor = None
        self.attempt = None
        self.lock = asyncio.Lock()

    def status(self):
        return {
            "state": self.state,
            "automatic_restarts": self.used,
            "restart_limit": self.restart_limit,
            "error_code": self.error_code,
        }

    def start(self):
        service = self.app.state.service
        if service.owns_execution and service.execution is not None:
            self.state = "ready" if service.execution_ready else "unavailable"
            self.monitor = asyncio.create_task(self._watch())

    async def _watch(self):
        while not self.closed and not self.app.state.exit_prepared:
            service = self.app.state.service
            process = getattr(getattr(service.execution, "host", None), "process", None)
            if self.state != "unavailable" and process is not None and process.poll() is not None:
                self.error_code = "HARNESS_PROCESS_EXITED"
                await self._recover()
            await asyncio.sleep(0.25)

    async def _recover(self):
        async with self.lock:
            await self._recover_locked()

    async def _recover_locked(self):
        service = self.app.state.service
        executor = self.app.state.task_executor
        async with self.app.state.submission_gate:
            if self.closed or self.app.state.exit_prepared:
                return
            service.execution_ready = False
            self.state = "recovering"
            executor._closing = True
        await executor.suspend_for_recovery()
        await service.close(reason="HARNESS_PROCESS_EXITED")
        await self.app.state.desktop_gateway.close()
        while (
            self.used < self.restart_limit and not self.closed and not self.app.state.exit_prepared
        ):
            delay = self.delays[min(self.used, len(self.delays) - 1)]
            self.used += 1
            await asyncio.sleep(delay)
            if self.closed or self.app.state.exit_prepared:
                return
            service.execution = None
            service.execution_error = None
            self.attempt = asyncio.create_task(service.open())
            try:
                await self.attempt
            except asyncio.CancelledError:
                raise
            except Exception:
                service.execution_ready = False
                service.execution_error = "HARNESS_RECOVERY_FAILED"
            finally:
                self.attempt = None
            if service.execution_ready:
                self.state, self.error_code = "ready", None
                await executor.resume_after_recovery()
                return
            self.error_code = service.execution_error or "HARNESS_RECOVERY_FAILED"
            await service.close()
        self.state = "unavailable"

    async def retry(self):
        if self.state in {"recovering", "disabled"} or self.lock.locked():
            return self.status()
        if self.closed or self.app.state.exit_prepared:
            return self.status()
        process = getattr(getattr(self.app.state.service.execution, "host", None), "process", None)
        if self.state == "ready" and process is not None and process.poll() is None:
            return self.status()
        self.used = 0
        await self._recover()
        return self.status()

    async def close(self):
        self.closed = True
        if self.attempt:
            self.attempt.cancel()
        if self.monitor:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
        if self.attempt:
            await asyncio.gather(self.attempt, return_exceptions=True)
