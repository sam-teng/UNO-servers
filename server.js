// UNO WebSocket Server
const WebSocket = require('ws');
const { v4: uuidv4 } = require('uuid');

const wss = new WebSocket.Server({ port: 8080 });

let rooms = {}; // { roomId: { players: { guid: ws } } }

wss.on('connection', (ws) => {
  const playerId = uuidv4();
  console.log(`玩家連線: ${playerId}`);

  ws.on('message', (msg) => {
    try {
      const data = JSON.parse(msg);
      switch (data.type) {
        case 'join':
          handleJoin(ws, playerId, data.roomId, data.guid);
          break;
        case 'playCard':
        case 'drawCard':
        case 'sayUno':
          broadcastToRoom(data.roomId, data, playerId);
          break;
        default:
          console.log("未知訊息:", data);
      }
    } catch (err) {
      console.error("解析訊息錯誤:", err);
    }
  });

  ws.on('close', () => {
    removePlayer(playerId);
  });
});

function handleJoin(ws, playerId, roomId, guid) {
  if (!rooms[roomId]) {
    rooms[roomId] = { players: {} };
  }
  rooms[roomId].players[playerId] = ws;

  console.log(`玩家 ${playerId} 加入房間 ${roomId}`);

  // 通知其他玩家
  broadcastToRoom(roomId, {
    type: 'playerJoined',
    playerId,
    guid,
  }, playerId);
}

function broadcastToRoom(roomId, msg, exceptId = null) {
  if (!rooms[roomId]) return;
  Object.entries(rooms[roomId].players).forEach(([pid, socket]) => {
    if (socket.readyState === WebSocket.OPEN && pid !== exceptId) {
      socket.send(JSON.stringify(msg));
    }
  });
}

function removePlayer(playerId) {
  for (const [roomId, room] of Object.entries(rooms)) {
    if (room.players[playerId]) {
      delete room.players[playerId];
      broadcastToRoom(roomId, {
        type: 'playerLeft',
        playerId,
      });
      console.log(`玩家 ${playerId} 離開房間 ${roomId}`);
    }
  }
}

console.log("UNO WebSocket Server 啟動於 ws://localhost:8080");
