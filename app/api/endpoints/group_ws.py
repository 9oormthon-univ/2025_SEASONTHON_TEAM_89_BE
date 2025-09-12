from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
import json
from app.services.websocket_manager import websocket_manager
from app.services.family_group_service import family_group_service
from app.schemas.family_group import (
    FamilyGroupCreateRequest,
    FamilyGroupJoinRequest,
    FamilyGroupKickMemberRequest
)

router = APIRouter()

@router.websocket("/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    """
    WebSocket 연결 엔드포인트
    
    - user_id: 연결하는 사용자 ID
    
    ### 클라이언트 → 서버 메시지:
    1. 그룹 생성: {"action": "create_group", "data": {"group_name": "...", "user_name": "..."}}
    2. 그룹 참여: {"action": "join_group", "data": {"join_code": "...", "user_name": "..."}}
    3. 멤버 추방: {"action": "kick_member", "data": {"target_user_id": "..."}}
    4. 그룹 완성: {"action": "complete_group", "data": {}}
    
    ### 서버 → 클라이언트 메시지:
    1. 그룹 생성됨: {"type": "group_created", "data": {...}}
    2. 그룹 참여됨: {"type": "joined_group", "data": {...}}
    3. 새 멤버 참여: {"type": "member_joined", "data": {...}}
    4. 멤버 추방됨: {"type": "member_kicked", "data": {...}}
    5. 추방당함: {"type": "kicked_from_group", "data": {...}}
    6. 그룹 완성: {"type": "group_completed", "data": {...}}
    7. 그룹 만료: {"type": "group_expired", "data": {...}}
    8. 에러: {"type": "error", "data": {"message": "...", "code": "..."}}
    """
    await websocket_manager.connect(websocket, user_id)
    
    try:
        while True:
            # 클라이언트로부터 메시지 수신
            data = await websocket.receive_text()
            message = json.loads(data)
            
            action = message.get("action")
            payload = message.get("data", {})
            
            try:
                if action == "create_group":
                    await handle_create_group(user_id, payload)
                    
                elif action == "join_group":
                    await handle_join_group(user_id, payload)
                    
                elif action == "kick_member":
                    await handle_kick_member(user_id, payload)
                    
                elif action == "complete_group":
                    await handle_complete_group(user_id)
                    
                else:
                    await websocket_manager.send_personal_message(user_id, {
                        "type": "error",
                        "data": {
                            "message": "알 수 없는 액션입니다.",
                            "code": "UNKNOWN_ACTION"
                        }
                    })
                    
            except ValueError as e:
                error_code = str(e)
                await websocket_manager.send_personal_message(user_id, {
                    "type": "error",
                    "data": {
                        "message": get_error_message(error_code),
                        "code": error_code
                    }
                })
            except Exception as e:
                await websocket_manager.send_personal_message(user_id, {
                    "type": "error",
                    "data": {
                        "message": "서버 오류가 발생했습니다.",
                        "code": "INTERNAL_ERROR"
                    }
                })
                
    except WebSocketDisconnect:
        websocket_manager.disconnect(user_id)

async def handle_create_group(user_id: str, payload: dict):
    """그룹 생성 처리"""
    request = FamilyGroupCreateRequest(
        user_id=user_id,
        user_name=payload.get("user_name", "")
    )
    
    result = family_group_service.create_family_group(request)
    
    # WebSocket 매니저에 그룹 생성 알림
    await websocket_manager.handle_group_creation(user_id, {
        "group_id": result.group_id,
        "join_code": result.join_code,
        "creator_id": result.creator_id,
        "created_at": result.created_at.isoformat()
    })

async def handle_join_group(user_id: str, payload: dict):
    """그룹 참여 처리"""
    request = FamilyGroupJoinRequest(
        join_code=payload.get("join_code", ""),
        user_id=user_id,
        user_name=payload.get("user_name", "")
    )
    
    result = family_group_service.join_family_group(request)
    
    # WebSocket 매니저에 멤버 참여 알림
    await websocket_manager.handle_member_join(
        request.join_code,
        user_id,
        {"user_name": request.user_name}
    )

async def handle_kick_member(user_id: str, payload: dict):
    """멤버 추방 처리"""
    request = FamilyGroupKickMemberRequest(
        user_id=user_id,
        target_user_id=payload.get("target_user_id", "")
    )
    
    result = family_group_service.kick_member_from_pending_group(
        request.user_id, 
        request.target_user_id
    )
    
    # 참여 코드 찾기
    pending_info = family_group_service.get_pending_group_info(user_id)
    if pending_info:
        join_code = pending_info["join_code"]
        
        # WebSocket 매니저에 멤버 추방 알림
        await websocket_manager.handle_member_kick(join_code, request.target_user_id, result)

async def handle_complete_group(user_id: str):
    """그룹 완성 처리"""
    # 완성 전 참여 코드 저장
    pending_info = family_group_service.get_pending_group_info(user_id)
    if not pending_info:
        raise ValueError("NO_PENDING_GROUP")
    
    join_code = pending_info["join_code"]
    
    result = await family_group_service.complete_group_creation(user_id)
    
    # WebSocket 매니저에 그룹 완성 알림
    await websocket_manager.handle_group_completion(join_code, result)

def get_error_message(error_code: str) -> str:
    """에러 코드에 따른 메시지 반환"""
    error_messages = {
        "USER_ALREADY_IN_GROUP": "이미 그룹에 속해있습니다.",
        "ALREADY_CREATING_GROUP": "이미 생성 중인 그룹이 있습니다.",
        "INVALID_JOIN_CODE": "유효하지 않은 참여 코드입니다.",
        "NO_PENDING_GROUP": "대기 중인 그룹이 없습니다.",
        "NOT_GROUP_CREATOR": "그룹 생성자만 이 작업을 수행할 수 있습니다.",
        "CANNOT_KICK_YOURSELF": "자기 자신을 추방할 수 없습니다.",
        "USER_NOT_IN_GROUP": "해당 사용자가 그룹에 없습니다."
    }
    return error_messages.get(error_code, "알 수 없는 오류가 발생했습니다.")
