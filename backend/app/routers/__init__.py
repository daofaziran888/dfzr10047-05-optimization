"""路由汇总：通用资源路由与超限恢复专用路由。"""
from app.routers import recovery, resource

ROUTERS = [recovery, resource]
