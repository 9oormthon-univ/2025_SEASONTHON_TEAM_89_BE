from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from app.config import settings
import logging

logger = logging.getLogger(__name__)

# MySQL 엔진 생성
engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,         # 연결 확인
    pool_recycle=3600,          # 1시간마다 연결 갱신
    pool_size=10,               # 연결 풀 크기
    max_overflow=20,            # 최대 추가 연결
    pool_timeout=30,            # 연결 대기 시간
    echo=settings.SQL_ECHO,     # SQL 로그 출력
    connect_args={
        "charset": "utf8mb4",
        "autocommit": False,
        "connect_timeout": 20
    }
)

# 세션 팩토리 생성
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base 클래스 생성
Base = declarative_base()

# 데이터베이스 의존성
def get_db():
    """데이터베이스 세션 의존성"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# 데이터베이스 연결 테스트
def test_connection():
    """데이터베이스 연결 테스트"""
    try:
        with engine.connect() as connection:
            result = connection.execute("SELECT 1")
            logger.info("데이터베이스 연결 성공")
            return True
    except Exception as e:
        logger.error(f"데이터베이스 연결 실패: {e}")
        return False

# 데이터베이스 초기화
def init_db():
    """데이터베이스 테이블 생성"""
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("데이터베이스 테이블 생성 완료")
    except Exception as e:
        logger.error(f"데이터베이스 테이블 생성 실패: {e}")
        raise