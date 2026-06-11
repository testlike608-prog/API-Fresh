import asyncio
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from ClientsClass import App as RobotApp


robot_app = RobotApp()
app = FastAPI(title="Fresh Fairino Cobot WebSocket API")


@app.on_event("shutdown")
def shutdown_app():
    robot_app.stop()


@app.websocket("/ws/robot")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    await _send(websocket, "connected", message="WebSocket API is ready")

    try:
        while True:
            message = await websocket.receive_json()
            action = message.get("action") or message.get("command")

            try:
                if action == "ping":
                    await _send(websocket, "pong")

                elif action == "start_camera":
                    camera_index = message.get("camera_index")
                    result = await asyncio.to_thread(robot_app.start, camera_index)
                    await _send(websocket, "camera_started", result=result)

                elif action == "connect_robot":
                    result = await asyncio.to_thread(
                        robot_app.connect_robot,
                        message.get("ip"),
                        message.get("enable", True),
                    )
                    await _send(websocket, "robot_connected", **result)

                elif action == "disconnect_robot":
                    result = await asyncio.to_thread(robot_app.disconnect_robot)
                    await _send(websocket, "robot_disconnected", **result)

                elif action == "robot_status":
                    result = await asyncio.to_thread(robot_app.robot_status)
                    await _send(websocket, "robot_status", **result)

                elif action == "capture":
                    result = await asyncio.to_thread(
                        robot_app.capture_image,
                        message.get("point_name", "manual"),
                        message.get("index", 1),
                        message.get("output_dir"),
                    )
                    await _send(websocket, "captured", **result)

                elif action == "move_point":
                    result = await asyncio.to_thread(
                        robot_app.move_to_point,
                        message.get("point", {}),
                        message.get("motion_type", "linear"),
                        message.get("tool", 0),
                        message.get("user", 0),
                        message.get("vel", 20.0),
                        message.get("acc", 0.0),
                        message.get("ovl", 100.0),
                    )
                    await _send(websocket, "move_completed", **result)

                elif action == "move_capture":
                    await _run_move_capture(websocket, message)

                elif action == "stop_robot":
                    result = await asyncio.to_thread(robot_app.stop_robot_motion)
                    await _send(websocket, "robot_stopped", **result)

                elif action == "stop":
                    await asyncio.to_thread(robot_app.stop)
                    await _send(websocket, "stopped")

                else:
                    await _send(websocket, "error", message=f"Unknown action: {action}")

            except Exception as exc:
                await _send(websocket, "error", message=str(exc))

    except WebSocketDisconnect:
        return


async def _run_move_capture(websocket: WebSocket, message: dict[str, Any]):
    loop = asyncio.get_running_loop()
    progress_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def progress_callback(event: dict[str, Any]):
        loop.call_soon_threadsafe(progress_queue.put_nowait, event)

    worker = asyncio.create_task(
        asyncio.to_thread(
            robot_app.move_points_and_capture,
            message.get("points", []),
            message.get("motion_type", "linear"),
            message.get("tool", 0),
            message.get("user", 0),
            message.get("vel", 20.0),
            message.get("acc", 0.0),
            message.get("ovl", 100.0),
            message.get("output_dir"),
            progress_callback,
        )
    )

    await _send(websocket, "job_started", action="move_capture")

    while True:
        progress_task = asyncio.create_task(progress_queue.get())
        done, pending = await asyncio.wait(
            {worker, progress_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if progress_task in done:
            await _send(websocket, "progress", **progress_task.result())
        else:
            progress_task.cancel()

        if worker in done:
            for pending_task in pending:
                pending_task.cancel()
            results = worker.result()
            await _send(websocket, "job_completed", action="move_capture", images=results)
            return


async def _send(websocket: WebSocket, event: str, **payload: Any):
    await websocket.send_json({"event": event, **payload})
