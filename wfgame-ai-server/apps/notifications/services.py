import json
import time
import uuid
from django.conf import settings
from enum import Enum
import logging

logger = logging.getLogger(__name__)

class SSEEvent(Enum):
    # 私信
    MESSAGE = 'message' 
    # 广播
    BROADCAST = 'broadcast'
    # OCR TASK 更新
    OCR_TASK_UPDATE = 'ocr_task_update'
    # 动作库更新
    ACTION_UPDATE = 'action_update'
    # 设备更新
    DEVICE_UPDATE = 'device_update'
    # OCR离线报告导出
    OCR_REPORT_EXPORT = 'ocr_report_export'


class SSEConnectionManager:
    """
    SSE连接管理器:用于追踪活跃的连接,使用Redis存储连接信息
    支持同一用户的多个连接[多个浏览器tab]
    """
    def __init__(self):
        self.redis_key_prefix = 'sse_connections:'  # Redis键前缀
        self.connection_hash_key = 'sse_active_connections'  # 存储所有连接的hash key
        self.user_connections_key_prefix = 'user_connections:'  # 用户连接列表key前缀
    
    def _get_redis_client(self):
        """获取Redis客户端实例"""
        return settings.REDIS.client
    
    def _get_user_connections_key(self, username):
        """获取用户连接列表的Redis key"""
        return f"{self.user_connections_key_prefix}{username}"
    
    def add_connection(self, username, connection_id=None):
        """
        添加新的SSE连接
        
        :param username: 用户名
        :param connection_id: 连接ID,如果不提供则自动生成
        :return: 连接ID
        """
        if connection_id is None:
            connection_id = str(uuid.uuid4())
            
        try:
            r = self._get_redis_client()
            current_time = time.time()
            connection_info = {
                'connection_id': connection_id,
                'username': username,
                'connection_time': current_time,
                'last_heartbeat': current_time
            }
            
            # 将连接信息存储到总的hash中
            r.hset(
                self.connection_hash_key,
                connection_id,
                json.dumps(connection_info, ensure_ascii=False)
            )
            
            # 将连接ID添加到用户的连接列表中
            user_connections_key = self._get_user_connections_key(username)
            r.sadd(user_connections_key, connection_id)
            
            # 设置过期时间（默认1小时）
            r.expire(self.connection_hash_key, 3600)
            r.expire(user_connections_key, 3600)
            
            logger.info(f"Added SSE connection {connection_id} for user {username}")
            return connection_id
            
        except Exception as e:
            logger.error(f"Error adding SSE connection for user {username}: {e}")
            return None
    
    def remove_connection(self, connection_id):
        """
        移除SSE连接
        
        :param connection_id: 连接ID
        """
        try:
            r = self._get_redis_client()
            
            # 先获取连接信息以获得用户名
            connection_data = r.hget(self.connection_hash_key, connection_id)
            if connection_data:
                connection_info = json.loads(connection_data)
                username = connection_info.get('username')
                
                # 从总hash中删除连接
                r.hdel(self.connection_hash_key, connection_id)
                
                # 从用户连接列表中删除
                if username:
                    user_connections_key = self._get_user_connections_key(username)
                    r.srem(user_connections_key, connection_id)
                    
                    # 如果用户没有其他连接了，删除用户连接列表
                    if r.scard(user_connections_key) == 0:
                        r.delete(user_connections_key)
                
                logger.info(f"Removed SSE connection {connection_id} for user {username}")
            else:
                logger.warning(f"Connection {connection_id} not found when removing")
            
        except Exception as e:
            logger.error(f"Error removing SSE connection {connection_id}: {e}")
    
    def update_heartbeat(self, connection_id):
        """
        更新连接的心跳时间
        
        :param connection_id: 连接ID
        """
        try:
            r = self._get_redis_client()
            
            # 获取现有连接信息
            existing_data = r.hget(self.connection_hash_key, connection_id)
            if existing_data:
                connection_info = json.loads(existing_data)
                connection_info['last_heartbeat'] = time.time()
                
                # 更新Redis中的连接信息
                r.hset(
                    self.connection_hash_key,
                    connection_id,
                    json.dumps(connection_info, ensure_ascii=False)
                )
                
                # 重新设置过期时间
                r.expire(self.connection_hash_key, 3600)
                
                # 更新用户连接列表的过期时间
                username = connection_info.get('username')
                if username:
                    user_connections_key = self._get_user_connections_key(username)
                    r.expire(user_connections_key, 3600)
                
        except Exception as e:
            logger.error(f"Error updating heartbeat for connection {connection_id}: {e}")
    
    def get_active_users(self):
        """获取所有活跃用户列表"""
        try:
            r = self._get_redis_client()
            active_connections = r.hgetall(self.connection_hash_key)
            
            # 从连接信息中提取唯一的用户名列表
            users = set()
            for connection_data in active_connections.values():
                try:
                    connection_info = json.loads(connection_data)
                    username = connection_info.get('username')
                    if username:
                        users.add(username)
                except (json.JSONDecodeError, KeyError):
                    continue
                    
            return list(users)
            
        except Exception as e:
            logger.error(f"Error getting active users: {e}")
            return []
    
    def get_user_connections(self, username):
        """
        获取指定用户的所有连接ID
        
        :param username: 用户名
        :return: 连接ID列表
        """
        try:
            r = self._get_redis_client()
            user_connections_key = self._get_user_connections_key(username)
            connection_ids = r.smembers(user_connections_key)
            return [conn_id.decode('utf-8') if isinstance(conn_id, bytes) else conn_id 
                    for conn_id in connection_ids]
        except Exception as e:
            logger.error(f"Error getting user connections for {username}: {e}")
            return []
    
    def has_active_connections(self, username):
        """
        检查用户是否有活跃连接
        
        :param username: 用户名
        :return: 布尔值
        """
        return len(self.get_user_connections(username)) > 0
    
    def get_connection_info(self, connection_id):
        """
        获取特定连接的信息
        
        :param connection_id: 连接ID
        :return: 连接信息字典或None
        """
        try:
            r = self._get_redis_client()
            connection_data = r.hget(self.connection_hash_key, connection_id)
            if connection_data:
                return json.loads(connection_data)
            return None
            
        except Exception as e:
            logger.error(f"Error getting connection info for {connection_id}: {e}")
            return None
    
    def get_user_connection_details(self, username):
        """
        获取指定用户的所有连接详细信息
        
        :param username: 用户名
        :return: 连接详细信息列表
        """
        try:
            connection_ids = self.get_user_connections(username)
            details = []
            
            for connection_id in connection_ids:
                connection_info = self.get_connection_info(connection_id)
                if connection_info:
                    details.append(connection_info)
            
            return details
            
        except Exception as e:
            logger.error(f"Error getting user connection details for {username}: {e}")
            return []
    
    def cleanup_stale_connections(self, timeout=300):  # 5分钟超时
        """清理过期的连接记录"""
        stale_connections = []
        stale_users = set()
        
        try:
            r = self._get_redis_client()
            current_time = time.time()
            
            # 获取所有连接信息
            active_connections = r.hgetall(self.connection_hash_key)
            
            for connection_id, connection_data in active_connections.items():
                try:
                    connection_info = json.loads(connection_data)
                    last_heartbeat = connection_info.get('last_heartbeat', 0)
                    username = connection_info.get('username')
                    
                    # 检查是否超时
                    if current_time - last_heartbeat > timeout:
                        stale_connections.append(connection_id)
                        if username:
                            stale_users.add(username)
                        
                except (json.JSONDecodeError, KeyError, TypeError) as e:
                    logger.error(f"Error parsing connection data for connection {connection_id}: {e}")
                    stale_connections.append(connection_id)  # 数据损坏的连接也清理掉
            
            # 批量删除过期连接
            if stale_connections:
                # 删除连接详情
                r.hdel(self.connection_hash_key, *stale_connections)
                
                # 清理用户连接列表
                for username in stale_users:
                    user_connections_key = self._get_user_connections_key(username)
                    # 移除过期的连接ID
                    for conn_id in stale_connections:
                        r.srem(user_connections_key, conn_id)
                    
                    # 如果用户没有其他连接了，删除用户连接列表
                    if r.scard(user_connections_key) == 0:
                        r.delete(user_connections_key)
                
                logger.info(f"Cleaned up {len(stale_connections)} stale connections for users: {list(stale_users)}")
                
        except Exception as e:
            logger.error(f"Error cleaning up stale connections: {e}")
        
        return {
            'stale_connections': stale_connections,
            'stale_users': list(stale_users),
            'connection_count': len(stale_connections),
            'user_count': len(stale_users)
        }

    def clear_all_connections(self):
        """
        清理所有SSE连接记录（服务重启时使用）
        
        :return: 清理结果统计
        """
        try:
            r = self._get_redis_client()
            
            # 获取所有活跃连接信息用于统计
            active_connections = r.hgetall(self.connection_hash_key)
            affected_users = set()
            
            # 统计涉及的用户
            for connection_data in active_connections.values():
                try:
                    connection_info = json.loads(connection_data)
                    username = connection_info.get('username')
                    if username:
                        affected_users.add(username)
                except (json.JSONDecodeError, KeyError):
                    continue
            
            # 删除主连接hash
            connection_count = r.hlen(self.connection_hash_key)
            r.delete(self.connection_hash_key)
            
            # 删除所有用户连接列表
            user_keys_deleted = 0
            if affected_users:
                user_keys = [self._get_user_connections_key(user) for user in affected_users]
                # 批量删除用户连接列表
                deleted_keys = r.delete(*user_keys)
                user_keys_deleted = deleted_keys
                
            logger.info(f"Service restart: Cleared all SSE connections - {connection_count} connections from {len(affected_users)} users")
            
            return {
                'connection_count': connection_count,
                'user_count': len(affected_users),
                'user_keys_deleted': user_keys_deleted,
                'affected_users': list(affected_users)
            }
            
        except Exception as e:
            logger.error(f"Error clearing all connections on service restart: {e}")
            return {
                'connection_count': 0,
                'user_count': 0,
                'user_keys_deleted': 0,
                'affected_users': [],
                'error': str(e)
            }

# 全局连接管理器实例
connection_manager = SSEConnectionManager()



def private_chanel(username):
    """旧版本兼容，用户级别的频道（所有连接共享）"""
    return f'user_{username}_notifications'

def private_chanel_by_connection(connection_id):
    """基于连接ID的私有频道"""
    return f'connection_{connection_id}_notifications'

def global_channel():
    return 'global_notifications'

def send_message(data, event, username=None, all_connections=True):
    """
    发送通知，可以是私信或广播。

    :param data: 要发送的数据 (可被JSON序列化的任何类型)
    :param event: 事件类型
    :param username: (可选) 用户ID。如果提供,则为私信;否则为广播。
    :param all_connections: 当发送私信时，是否向用户的所有连接发送。
                                       True: 向所有连接发送 (默认)
                                       False: 仅向第一个连接发送
    """
    # 如果是私信，检查用户是否有活跃连接
    if username and not connection_manager.has_active_connections(username):
        logger.warning(f"User {username} has no active SSE connection, skipping message")
        return False
    
    success_count = 0
    
    if username:
        # 私信模式
        user_connections = connection_manager.get_user_connections(username)
        
        if not user_connections:
            logger.warning(f"No active connections found for user {username}")
            return False
        
        # 根据参数决定发送到所有连接还是只发送到第一个连接
        target_connections = user_connections if all_connections else [user_connections[0]]
        
        try:
            r = settings.REDIS.client
            json_data = json.dumps(data, ensure_ascii=False)
            formatted_message = f"event: {event}\ndata: {json_data}\n\n"
            
            # 为每个连接发送到专属频道
            for connection_id in target_connections:
                channel = private_chanel_by_connection(connection_id)
                result = r.publish(channel, formatted_message)
                
                if result > 0:
                    success_count += 1
                else:
                    logger.warning(f"No subscribers for connection channel {channel}")
            
            logger.info(f"Sent message to {success_count}/{len(target_connections)} connections for user {username}")
            
        except Exception as e:
            logger.error(f"Error sending private message to user {username}: {e}")
            return False
    
    else:
        # 广播模式
        channel = global_channel()
        
        try:
            r = settings.REDIS.client
            json_data = json.dumps(data, ensure_ascii=False)
            formatted_message = f"event: {event}\ndata: {json_data}\n\n"
            result = r.publish(channel, formatted_message)
            
            # 如果没有订阅者接收消息，记录日志
            if result == 0:
                logger.warning(f"No subscribers for global channel {channel}")
            else:
                success_count = result
                logger.info(f"Broadcast message sent to {result} subscribers")
            
        except Exception as e:
            logger.error(f"Error sending broadcast message: {e}")
            return False
    
    return success_count > 0

def send_notification(username, data, event=SSEEvent.MESSAGE.value, all_connections=True):
    """
    向特定用户发送通知 (私信)。

    :param username: 用户ID
    :param data: 要发送的数据
    :param event: 事件类型
    :param all_connections: 是否向用户的所有连接发送
    """
    return send_message(data, event, username=username, all_connections=all_connections)

def broadcast_notification(data, event=SSEEvent.BROADCAST.value):
    """
    向所有用户广播通知。

    :param data: 要发送的数据
    :param event: 事件类型
    """
    return send_message(data, event)

def get_active_sse_connections():
    """
    获取当前活跃的SSE连接信息
    """
    try:
        active_users = connection_manager.get_active_users()
        
        # 获取详细的连接信息
        connection_details = {}
        total_connections = 0
        
        for username in active_users:
            user_connections = connection_manager.get_user_connection_details(username)
            connection_details[username] = user_connections
            total_connections += len(user_connections)
        
        return {
            'active_users': active_users,
            'user_count': len(active_users),
            'total_connections': total_connections,
            'connection_details': connection_details
        }
    except Exception as e:
        logger.error(f"Error getting active SSE connections: {e}")
        return {
            'active_users': [],
            'user_count': 0,
            'total_connections': 0,
            'connection_details': {}
        }

def cleanup_stale_sse_connections():
    """
    清理过期的SSE连接记录（兼容性函数，推荐直接使用 connection_manager.cleanup_stale_connections()）
    """
    return connection_manager.cleanup_stale_connections()

def clear_all_sse_connections_on_startup():
    """
    服务启动时清理所有SSE连接记录
    这是必要的，因为服务重启后Redis中的连接信息会变成僵尸数据
    
    :return: 清理结果
    """
    logger.info("Service starting up - clearing all SSE connections from Redis...")
    result = connection_manager.clear_all_connections()
    
    if result.get('error'):
        logger.error(f"Failed to clear connections on startup: {result['error']}")
    else:
        logger.info(f"Startup cleanup completed - removed {result['connection_count']} connections from {result['user_count']} users")
    
    return result
