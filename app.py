import os
import json
import random
import threading
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

from flask import Flask, jsonify, request
from flask_sock import Sock

# =========================
# Flask / Sock 基本設定
# =========================
app = Flask(__name__)
sock = Sock(app)

@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200

@app.route("/")
def index():
    return "UNO WebSocket Server is running. Use /ws for WebSocket."

# =========================
# UNO 資料結構與工具
# =========================
COLORS = ["red", "yellow", "green", "blue", "wild"]
VALUES = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "skip", "reverse", "drawTwo", "wild", "wildDrawFour"
]

def card_point(value: str) -> int:
    if value in ["skip", "reverse", "drawTwo"]:
        return 20
    if value in ["wild", "wildDrawFour"]:
        return 50
    # 0~9
    try:
        idx = VALUES.index(value)
        return min(idx, 9)  # one..nine 對應 1..9，zero 計 0
    except Exception:
        return 0

@dataclass
class Card:
    color: str
    value: str

    def to_json(self):
        return {"color": self.color, "value": self.value}

    @staticmethod
    def from_json(j):
        return Card(color=j["color"], value=j["value"])

    @property
    def is_wild(self) -> bool:
        return self.value in ("wild", "wildDrawFour")

@dataclass
class Player:
    id: str
    name: str
    ws: object = None
    hand: List[Card] = field(default_factory=list)
    score: int = 0
    said_uno: bool = False
    connected: bool = True

    def public(self):
        # 手牌數量公開、內容不公開
        return {
            "id": self.id,
            "name": self.name,
            "handCount": len(self.hand),
            "score": self.score,
            "connected": self.connected,
        }

@dataclass
class Rules:
    stackingPlus: bool = True   # +2/+4 疊加
    skipChain: bool = True      # 跳過連打（此範例保留旗標，行為以標準跳過為主）

    def to_json(self):
        return {"stackingPlus": self.stackingPlus, "skipChain": self.skipChain}

@dataclass
class Room:
    id: str
    name: str
    rules: Rules = field(default_factory=Rules)
    players: Dict[str, Player] = field(default_factory=dict)
    draw_pile: List[Card] = field(default_factory=list)
    discard_pile: List[Card] = field(default_factory=list)
    current_color: Optional[str] = None
    current_value: Optional[str] = None
    current_player_idx: int = 0
    direction: int = 1
    accumulated_draw: int = 0
    started: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)

    # ---------- 遊戲牌庫 ----------
    def build_deck(self):
        self.draw_pile.clear()
        self.discard_pile.clear()
        # 四色：0x1、1-9x2、skip/reverse/drawTwo 各2
        for color in ["red", "yellow", "green", "blue"]:
            self.draw_pile.append(Card(color, "zero"))
            for v in ["one","two","three","four","five","six","seven","eight","nine"]:
                self.draw_pile.append(Card(color, v))
                self.draw_pile.append(Card(color, v))
            for v in ["skip","reverse","drawTwo"]:
                self.draw_pile.append(Card(color, v))
                self.draw_pile.append(Card(color, v))
        # wild / wildDrawFour 各4
        for _ in range(4):
            self.draw_pile.append(Card("wild", "wild"))
            self.draw_pile.append(Card("wild", "wildDrawFour"))
        random.shuffle(self.draw_pile)

    def draw(self) -> Card:
        if not self.draw_pile:
            # 牌堆用完 → 從棄牌堆（保留頂牌）洗回來
            if len(self.discard_pile) > 1:
                top = self.discard_pile.pop()
                self.draw_pile = self.discard_pile[:]
                self.discard_pile = [top]
                random.shuffle(self.draw_pile)
        return self.draw_pile.pop()

    def deal(self):
        for p in self.players.values():
            p.hand.clear()
            p.said_uno = False
        for _ in range(7):
            for p in self.iter_players_order():
                p.hand.append(self.draw())
        # 翻第一張（不可是 wild）
        first = self.draw()
        while first.is_wild:
            self.draw_pile.append(first)
            random.shuffle(self.draw_pile)
            first = self.draw()
        self.discard_pile.append(first)
        self.current_color = first.color
        self.current_value = first.value
        self.current_player_idx = 0
        self.direction = 1
        self.accumulated_draw = 0
        self.apply_flip(first)

    def iter_players_order(self) -> List[Player]:
        return list(self.players.values())

    def ordered_ids(self) -> List[str]:
        return [p.id for p in self.iter_players_order()]

    def next_index(self, base_idx: Optional[int] = None, step: int = 1) -> int:
        ids = self.ordered_ids()
        n = len(ids)
        if n == 0:
            return 0
        i = self.current_player_idx if base_idx is None else base_idx
        i = (i + self.direction * step) % n
        return i

    def current_player(self) -> Optional[Player]:
        ids = self.ordered_ids()
        if not ids:
            return None
        return self.players[ids[self.current_player_idx]]

    def player_by_id(self, pid: str) -> Optional[Player]:
        return self.players.get(pid)

    # ---------- 規則/合法性 ----------
    def can_play(self, card: Card) -> bool:
        if self.accumulated_draw > 0:
            if not self.rules.stackingPlus:
                return False
            # 僅允許繼續疊加同類型（+2 / +4）
            return (card.value == "drawTwo" and self.current_value == "drawTwo") or \
                   (card.value == "wildDrawFour" and self.current_value == "wildDrawFour")
        return card.is_wild or card.color == self.current_color or card.value == self.current_value

    def apply_flip(self, first: Card):
        # 翻開第一張時的效果
        if first.value == "skip":
            self.current_player_idx = self.next_index(step=1)
        elif first.value == "reverse":
            self.direction *= -1
            if len(self.players) == 2:
                self.current_player_idx = self.next_index(step=1)
        elif first.value == "drawTwo":
            nxt = self.next_index(step=1)
            self.draw_cards(nxt, 2)
            self.current_player_idx = self.next_index(step=1)

    def draw_cards(self, player_idx: int, n: int):
        target = self.players[self.ordered_ids()[player_idx]]
        for _ in range(n):
            target.hand.append(self.draw())

    def broadcast(self, msg: dict, include_self: bool = True, only_ids: Optional[List[str]] = None):
        data = json.dumps(msg, separators=(",", ":"))
        for pid, p in self.players.items():
            if only_ids is not None and pid not in only_ids:
                continue
            if p.ws is None:
                continue
            if not include_self and msg.get("sender") == pid:
                continue
            try:
                p.ws.send(data)
            except Exception:
                # 忽略暫時送不出去
                pass

    def public_state(self):
        top = self.discard_pile[-1] if self.discard_pile else None
        return {
            "type": "state",
            "roomId": self.id,
            "name": self.name,
            "rules": self.rules.to_json(),
            "players": [p.public() for p in self.iter_players_order()],
            "topCard": top.to_json() if top else None,
            "currentColor": self.current_color,
            "currentValue": self.current_value,
            "currentPlayerId": self.current_player().id if self.current_player() else None,
            "direction": self.direction,
            "accumulatedDraw": self.accumulated_draw,
            "started": self.started,
        }

# =========================
# 全域房間管理
# =========================
rooms: Dict[str, Room] = {}
rooms_lock = threading.RLock()

def get_or_create_room(room_id: str, room_name: Optional[str] = None) -> Room:
    with rooms_lock:
        if room_id in rooms:
            return rooms[room_id]
        r = Room(id=room_id, name=room_name or room_id)
        rooms[room_id] = r
        return r

def remove_ws_from_rooms(ws):
    # 玩家斷線：標記 disconnected；若房間空了可選擇清理
    with rooms_lock:
        empty_rooms = []
        for room in rooms.values():
            with room.lock:
                for p in room.players.values():
                    if p.ws is ws:
                        p.connected = False
                        p.ws = None
                        room.broadcast({"type": "playerLeft", "playerId": p.id})
                # 若所有玩家都斷線，可回收（也可選擇保留一段時間）
                if all(not pl.connected for pl in room.players.values()):
                    empty_rooms.append(room.id)
        for rid in empty_rooms:
            rooms.pop(rid, None)

# =========================
# WebSocket 主邏輯
# =========================
def ws_send(ws, obj):
    ws.send(json.dumps(obj, separators=(",", ":")))

@sock.route("/ws")
def ws_handler(ws):
    """
    訊息協定（JSON）：
    1) 加入房間：
       {"type":"join","roomId":"room1","name":"Alice","playerId":"<guid>"}
    2) 設定規則（房主/任何人都可，依需求自行限制）：
       {"type":"setRules","roomId":"room1","rules":{"stackingPlus":true,"skipChain":true}}
    3) 開始遊戲：
       {"type":"start","roomId":"room1"}
    4) 出牌：
       {"type":"playCard","roomId":"room1","playerId":"...","card":{"color":"red","value":"five"},"chooseColor":"blue"(可選)}
    5) 抽牌：
       {"type":"drawCard","roomId":"room1","playerId":"..."}
    6) 喊 UNO：
       {"type":"sayUno","roomId":"room1","playerId":"..."}
    7) 檢舉沒喊 UNO：
       {"type":"calloutUno","roomId":"room1","playerId":"..."}  # 呼叫者
    8) 取得狀態（可選）：
       {"type":"getState","roomId":"room1"}
    """
    try:
        while True:
            raw = ws.receive()
            if raw is None:
                break
            try:
                data = json.loads(raw)
            except Exception:
                ws_send(ws, {"type": "error", "error": "invalid_json"})
                continue

            msg_type = data.get("type")
            room_id = data.get("roomId")
            if not msg_type or not room_id:
                ws_send(ws, {"type": "error", "error": "missing_type_or_roomId"})
                continue

            room = get_or_create_room(room_id)
            with room.lock:
                if msg_type == "join":
                    name = data.get("name") or "Player"
                    pid = data.get("playerId")
                    if not pid:
                        ws_send(ws, {"type": "error", "error": "missing_playerId"})
                        continue
                    # 新/舊玩家
                    if pid not in room.players:
                        room.players[pid] = Player(id=pid, name=name, ws=ws, connected=True)
                    else:
                        # 回來了
                        room.players[pid].ws = ws
                        room.players[pid].name = name
                        room.players[pid].connected = True

                    ws_send(ws, {"type":"joined","roomId":room_id,"playerId":pid})
                    room.broadcast({"type":"playerJoined","player":room.players[pid].public()}, only_ids=[x for x in room.players.keys() if x != pid])
                    # 回傳目前狀態
                    ws_send(ws, room.public_state())

                elif msg_type == "setRules":
                    rj = data.get("rules") or {}
                    room.rules.stackingPlus = bool(rj.get("stackingPlus", room.rules.stackingPlus))
                    room.rules.skipChain = bool(rj.get("skipChain", room.rules.skipChain))
                    room.broadcast({"type":"rulesUpdated","rules":room.rules.to_json()})

                elif msg_type == "start":
                    if len(room.players) < 2:
                        ws_send(ws, {"type":"error","error":"need_at_least_two_players"})
                        continue
                    room.build_deck()
                    room.deal()
                    room.started = True
                    room.broadcast(room.public_state())

                elif msg_type == "getState":
                    ws_send(ws, room.public_state())

                elif msg_type == "playCard":
                    if not room.started:
                        ws_send(ws, {"type":"error","error":"game_not_started"})
                        continue
                    pid = data.get("playerId")
                    if not pid or pid not in room.players:
                        ws_send(ws, {"type":"error","error":"invalid_player"})
                        continue
                    # 輪到？
                    current_id = room.current_player().id if room.current_player() else None
                    if current_id != pid:
                        ws_send(ws, {"type":"error","error":"not_your_turn"})
                        continue
                    # 卡片合法？
                    card = Card.from_json(data["card"])
                    hand = room.players[pid].hand
                    # 找到同色同值的卡（避免相同牌面多張混淆）
                    found_idx = next((i for i,c in enumerate(hand) if c.color==card.color and c.value==card.value), -1)
                    if found_idx == -1:
                        ws_send(ws, {"type":"error","error":"card_not_in_hand"})
                        continue
                    if not room.can_play(card):
                        ws_send(ws, {"type":"error","error":"illegal_move"})
                        continue

                    # 移除，放到棄牌
                    played = hand.pop(found_idx)
                    room.discard_pile.append(played)
                    # RESET 玩家 UNO 宣告狀態（當手牌數=1時需要重喊）
                    if len(hand) == 1:
                        room.players[pid].said_uno = False

                    # wild 選色
                    choose_color = data.get("chooseColor")
                    if played.is_wild:
                        room.current_color = choose_color or room.current_color
                        room.current_value = played.value
                        if played.value == "wildDrawFour":
                            room.accumulated_draw += 4
                            room.current_player_idx = room.next_index(step=1)
                            # 被加牌的人若不疊加（或規則不允許），在 drawCard 處理
                        else:
                            room.current_player_idx = room.next_index(step=1)
                    else:
                        room.current_color = played.color
                        room.current_value = played.value
                        if played.value == "skip":
                            # 跳過下一位
                            room.current_player_idx = room.next_index(step=2)
                        elif played.value == "reverse":
                            room.direction *= -1
                            if len(room.players) == 2:
                                room.current_player_idx = room.next_index(step=1)
                            else:
                                room.current_player_idx = room.next_index(step=1)
                        elif played.value == "drawTwo":
                            if room.rules.stackingPlus:
                                room.accumulated_draw += 2
                                room.current_player_idx = room.next_index(step=1)
                            else:
                                nxt = room.next_index(step=1)
                                room.draw_cards(nxt, 2)
                                room.current_player_idx = room.next_index(step=1)
                        else:
                            room.current_player_idx = room.next_index(step=1)

                    # 勝負判定
                    winner_id = None
                    if len(hand) == 0:
                        winner_id = pid
                        # 計分
                        total = 0
                        for oid, op in room.players.items():
                            if oid == pid:
                                continue
                            for c in op.hand:
                                total += card_point(c.value)
                        room.players[pid].score += total
                        room.started = False  # 一局結束

                    # 廣播狀態/事件
                    room.broadcast({
                        "type":"played",
                        "playerId": pid,
                        "card": played.to_json(),
                        "chooseColor": choose_color,
                        "state": room.public_state(),
                        "winnerId": winner_id
                    })

                elif msg_type == "drawCard":
                    if not room.started:
                        ws_send(ws, {"type":"error","error":"game_not_started"})
                        continue
                    pid = data.get("playerId")
                    if not pid or pid not in room.players:
                        ws_send(ws, {"type":"error","error":"invalid_player"})
                        continue
                    if (room.current_player().id if room.current_player() else None) != pid:
                        ws_send(ws, {"type":"error","error":"not_your_turn"})
                        continue

                    # 若有累積抽牌，現在必須結清（除非玩家選擇疊加在 playCard）
                    if room.accumulated_draw > 0:
                        idx = room.current_player_idx
                        room.draw_cards(idx, room.accumulated_draw)
                        room.accumulated_draw = 0
                        room.current_player_idx = room.next_index(step=1)
                    else:
                        # 一般抽一張後結束回合
                        idx = room.current_player_idx
                        room.draw_cards(idx, 1)
                        room.current_player_idx = room.next_index(step=1)

                    room.broadcast({
                        "type":"drew",
                        "playerId": pid,
                        "state": room.public_state()
                    })

                elif msg_type == "sayUno":
                    pid = data.get("playerId")
                    if pid in room.players:
                        # 僅當手牌=1 時有效
                        if len(room.players[pid].hand) == 1:
                            room.players[pid].said_uno = True
                        room.broadcast({"type":"saidUno","playerId":pid})

                elif msg_type == "calloutUno":
                    caller = data.get("playerId")
                    offenders = []
                    for p in room.players.values():
                        if len(p.hand) == 1 and not p.said_uno:
                            offenders.append(p.id)
                            p.hand.append(room.draw())
                            p.hand.append(room.draw())
                    if offenders:
                        room.broadcast({"type":"unoPenalty","offenders":offenders, "caller":caller, "state": room.public_state()})
                    else:
                        ws_send(ws, {"type":"unoPenalty","offenders":[]})

                else:
                    ws_send(ws, {"type":"error","error":"unknown_type"})

    finally:
        # 連線結束
        remove_ws_from_rooms(ws)

# =========================
# 本地啟動（Render 也會用 gunicorn）
# =========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
