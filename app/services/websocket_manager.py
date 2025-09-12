from fastapi import WebSocket, WebSocketDisconnect
from typing import Dict, Set, List
import json
import asyncio
from datetime import datetime
from app.services.family_group_service import family_group_service

class WebSocketManager:
    def __init__(self):
        # 연결된 클라이언트들
        self.active_connections: Dict[str, WebSocket] = {}  # user_id -> websocket
        # 그룹별 참여자 매핑
        self.group_members: Dict[str, Set[str]] = {}  # join_code -> set of user_ids
        
    async def connect(self, websocket: WebSocket, user_id: str):
        """WebSocket 연결 수락"""
        await websocket.accept()
        self.active_connections[user_id] = websocket
        
        # 사용자가 이미 대기 중인 그룹이 있는지 확인
        pending_info = family_group_service.get_pending_group_info(user_id)
        if pending_info:
            join_code = pending_info["join_code"]
            if join_code not in self.group_members:
                self.group_members[join_code] = set()
            self.group_members[join_code].add(user_id)
            
            # 기존 그룹 상태를 클라이언트에게 전송
            await self.send_personal_message(user_id, {
                "type": "group_status",
                "data": pending_info
            })
    
    def disconnect(self, user_id: str):
        """WebSocket 연결 해제"""
        if user_id in self.active_connections:
            del self.active_connections[user_id]
            
        # 그룹에서도 제거
        for join_code, members in self.group_members.items():
            if user_id in members:
                members.remove(user_id)
                break
    
    async def send_personal_message(self, user_id: str, message: dict):
        """특정 사용자에게 메시지 전송"""
        if user_id in self.active_connections:
            try:
                await self.active_connections[user_id].send_text(json.dumps(message))
            except:
                # 연결이 끊어진 경우
                self.disconnect(user_id)
    
    async def broadcast_to_group(self, join_code: str, message: dict, exclude_user: str = None):
        """그룹의 모든 멤버에게 메시지 브로드캐스트"""
        if join_code in self.group_members:
            for user_id in self.group_members[join_code].copy():
                if user_id != exclude_user:
                    await self.send_personal_message(user_id, message)
    
    async def handle_group_creation(self, user_id: str, group_data: dict):
        """그룹 생성 시 처리"""
        join_code = group_data["join_code"]
        
        # 그룹 멤버에 생성자 추가
        if join_code not in self.group_members:
            self.group_members[join_code] = set()
        self.group_members[join_code].add(user_id)
        
        # 생성자에게 그룹 생성 완료 메시지 전송
        await self.send_personal_message(user_id, {
            "type": "group_created",
            "data": group_data
        })
    
    async def handle_member_join(self, join_code: str, user_id: str, user_data: dict):
        """멤버 참여 시 처리"""
        # 그룹 멤버에 추가
        if join_code not in self.group_members:
            self.group_members[join_code] = set()
        self.group_members[join_code].add(user_id)
        
        # 새 멤버에게 그룹 정보 전송
        pending_info = family_group_service.get_pending_group_info(user_id)
        if pending_info:
            await self.send_personal_message(user_id, {
                "type": "joined_group",
                "data": pending_info
            })
        
        # 다른 멤버들에게 새 멤버 참여 알림
        await self.broadcast_to_group(join_code, {
            "type": "member_joined",
            "data": {
                "user_id": user_id,
                "user_name": user_data.get("user_name"),
                "joined_at": datetime.now().isoformat()
            }
        }, exclude_user=user_id)
    
    async def handle_member_kick(self, join_code: str, kicked_user_id: str, kick_data: dict):
        """멤버 추방 시 처리"""
        # 추방된 사용자에게 알림
        await self.send_personal_message(kicked_user_id, {
            "type": "kicked_from_group",
            "data": kick_data
        })
        
        # 그룹에서 제거
        if join_code in self.group_members:
            self.group_members[join_code].discard(kicked_user_id)
        
        # 다른 멤버들에게 추방 알림
        await self.broadcast_to_group(join_code, {
            "type": "member_kicked",
            "data": kick_data
        })
    
    async def handle_group_completion(self, join_code: str, completion_data: dict):
        """그룹 완성 시 처리"""
        # 모든 멤버에게 그룹 완성 알림
        await self.broadcast_to_group(join_code, {
            "type": "group_completed",
            "data": completion_data
        })
        
        # 그룹 멤버 매핑 정리
        if join_code in self.group_members:
            del self.group_members[join_code]
    
    async def handle_group_expiration(self, join_code: str):
        """그룹 만료 시 처리"""
        # 모든 멤버에게 만료 알림
        await self.broadcast_to_group(join_code, {
            "type": "group_expired",
            "data": {
                "message": "그룹 생성 시간이 만료되었습니다.",
                "expired_at": datetime.now().isoformat()
            }
        })
        
        # 그룹 멤버 매핑 정리
        if join_code in self.group_members:
            del self.group_members[join_code]

# 싱글톤 인스턴스
websocket_manager = WebSocketManager()
