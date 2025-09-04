import asyncio
import json
import os
import websockets
from websockets.exceptions import ConnectionClosed

rooms = {}  # { room_name: { "players": { player_id: websocket, ... }, "info": { id,name,isBot } } }

async def handler(websocket, path):
    player_id = None
    room_name = None

    try:
        async for message in websocket:
            data = json.loads(message)
            action = data.get("action")

            if action == "join":
                player = data["player"]
                player_id = player["id"]
                room_name = "default"  # 目前先固定房間，之後可改成 data["room"]
                if room_name not in rooms:
                    rooms[room_name] = {"players": {}, "info": {}}

                rooms[room_name]["players"][player_id] = websocket
                rooms[room_name]["info"][player_id] = player

                print(f"玩家 {player_id} 加入房間 {room_name}")

                # 廣播房間狀態
                await broadcast_room_state(room_name)

            elif action in ("play_card", "draw_one", "round_end"):
                if room_name:
                    await broadcast(room_name, data, exclude=player_id)

    except ConnectionClosed:
        print(f"玩家斷線: {player_id}")
    finally:
        if room_name and player_id:
            rooms[room_name]["players"].pop(player_id, None)
            rooms[room_name]["info"].pop(player_id, None)
            await broadcast_room_state(room_name)

async def broadcast(room_name, message, exclude=None):
    if room_name not in rooms:
        return
    dead = []
    for pid, ws in rooms[room_name]["players"].items():
        if pid == exclude:
            continue
        try:
            await ws.send(json.dumps(message))
        except Exception:
            dead.append(pid)
    # 清理死連線
    for pid in dead:
        rooms[room_name]["players"].pop(pid, None)
        rooms[room_name]["info"].pop(pid, None)

async def broadcast_room_state(room_name):
    players = list(rooms[room_name]["info"].values())
    message = {"action": "room_state", "players": players}
    await broadcast(room_name, message)

async def healthcheck(websocket, path):
    await websocket.send(json.dumps({"status": "ok"}))

async def main():
    port = int(os.environ.get("PORT", 3000))
    async with websockets.serve(handler, "0.0.0.0", port):
        print(f"UNO Server listening on port {port}")
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())
