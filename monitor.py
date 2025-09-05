import os
import time
import psutil
import requests
import logging
import logging.handlers
import json
import yaml
import threading
import asyncio
import subprocess
import sys
from datetime import datetime
from typing import Dict, List, Tuple, Optional, Any
from enum import Enum

try:
    from dotenv import load_dotenv, find_dotenv
    DOTENV_AVAILABLE = True
except ImportError:
    DOTENV_AVAILABLE = False

try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
    from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False


class AlertLevel(Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    ERROR = "ERROR"


class Alert:
    def __init__(self, level: AlertLevel, resource: str, message: str, 
                 value: float, threshold: float, timestamp: datetime):
        self.level = level
        self.resource = resource
        self.message = message
        self.value = value
        self.threshold = threshold
        self.timestamp = timestamp

    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level.value,
            "resource": self.resource,
            "message": self.message,
            "value": self.value,
            "threshold": self.threshold,
            "timestamp": self.timestamp.isoformat()
        }


class ResourceMonitor:
    def __init__(self, config_path: str = "config.yaml"):
        self.start_time = time.time()
        self.config_path = config_path
        self.config = self.load_config(config_path)
        self.setup_logging()

        self.prev_net_io = psutil.net_io_counters()
        self.prev_check_time = time.time()
        self.alert_history: List[Alert] = []
        self.monitoring_enabled = True
        self.monitoring_thread = None
        self.telegram_thread = None
        
        self.last_alert_times = {}
        
        self.current_metrics = {
            "cpu_percent": 0,
            "memory_percent": 0,
            "disk_percent": 0,
            "network_sent_speed": 0,
            "network_recv_speed": 0
        }
        
        self.stats = {
            "checks": 0,
            "alerts_triggered": 0,
            "notifications_sent": 0,
            "start_time": datetime.now()
        }
        
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        
        self.load_state()
        
        logging.info("Мониторинг ресурсов инициализирован")
        
    def load_config(self, config_path: str) -> Dict[str, Any]:
        if not os.path.exists(config_path):
            logging.info(f"Файл конфигурации {config_path} не найден, создаем новый...")
            return self.create_default_config(config_path)
            
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}
                
            telegram_config = config.get("notifications", {}).get("telegram", {})
            if telegram_config.get("enabled", False):
                bot_token = telegram_config.get("bot_token", "")
                chat_id = telegram_config.get("chat_id", "")
                
                if not bot_token or bot_token == "your_bot_token_here":
                    logging.warning("Токен бота не задан или имеет значение по умолчанию")
                    config["notifications"]["telegram"]["enabled"] = False
                    
                if not chat_id or chat_id == "your_chat_id_here":
                    logging.warning("Chat ID не задан или имеет значение по умолчанию")
                    config["notifications"]["telegram"]["enabled"] = False
            
            if "logging" not in config:
                config["logging"] = {
                    "level": "INFO",
                    "file": "monitor.log",
                    "max_size_mb": 10,
                    "backup_count": 5
                }
                
            if "monitoring" not in config:
                config["monitoring"] = {
                    "check_interval_seconds": 60,
                    "notification_cooldown_seconds": 300,
                    "resources": {
                        "cpu": {"enabled": True, "threshold": 80},
                        "memory": {"enabled": True, "threshold": 80},
                        "disk": {"enabled": True, "threshold": 80, "paths": ["/"]},
                        "network": {"enabled": True, "threshold_sent_mbps": 10, "threshold_recv_mbps": 10}
                    }
                }
                
            if "notifications" not in config:
                config["notifications"] = {
                    "telegram": {
                        "enabled": True, 
                        "bot_token": "your_bot_token_here",
                        "chat_id": "your_chat_id_here"
                    }
                }
                
            self.save_config(config, config_path)
                    
            return config
        except Exception as e:
            logging.error(f"Ошибка загрузки конфигурации: {e}")
            return self.create_default_config(config_path)
            
    def save_config(self, config: Dict[str, Any], config_path: str):
        try:
            os.makedirs(os.path.dirname(os.path.abspath(config_path)), exist_ok=True)
            with open(config_path, 'w', encoding='utf-8') as f:
                yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
            logging.info(f"Конфигурация сохранена в {config_path}")
        except Exception as e:
            logging.error(f"Ошибка сохранения конфигурации: {e}")
            try:
                backup_path = os.path.join(os.getcwd(), os.path.basename(config_path))
                with open(backup_path, 'w', encoding='utf-8') as f:
                    yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
                logging.info(f"Создана резервная копия конфигурации в {backup_path}")
            except Exception as backup_error:
                logging.error(f"Не удалось создать резервную копию конфигурации: {backup_error}")
            
    def create_default_config(self, config_path: str) -> Dict[str, Any]:
        default_config = {
            "logging": {
                "level": "INFO",
                "file": "monitor.log",
                "max_size_mb": 10,
                "backup_count": 5
            },
            "monitoring": {
                "check_interval_seconds": 60,
                "notification_cooldown_seconds": 300,
                "resources": {
                    "cpu": {"enabled": True, "threshold": 80},
                    "memory": {"enabled": True, "threshold": 80},
                    "disk": {"enabled": True, "threshold": 80, "paths": ["/"]},
                    "network": {"enabled": True, "threshold_sent_mbps": 10, "threshold_recv_mbps": 10}
                }
            },
            "notifications": {
                "telegram": {
                    "enabled": True,  
                    "bot_token": "your_bot_token_here",
                    "chat_id": "your_chat_id_here"
                }
            }
        }
        
        self.save_config(default_config, config_path)
            
        logging.info(f"Создан файл конфигурации по умолчанию: {config_path}")
        logging.info("Пожалуйста, настройте конфигурацию перед использованием")
        
        print("\n" + "="*60)
        print("СОЗДАН КОНФИГУРАЦИОННЫЙ ФАЙЛ ПО УМОЛЧАНИЮ")
        print("="*60)
        print("Для настройки Telegram уведомлений:")
        print("1. Создайте бота через @BotFather")
        print("2. Получите свой chat ID через @userinfobot")
        print("3. Отредактируйте файл config.yaml:")
        print("   - Установите enabled: true")
        print("   - Замените your_bot_token_here на полученный токен")
        print("   - Замените your_chat_id_here на полученный chat ID")
        print("4. Перезапустите приложение")
        print("="*60 + "\n")
        
        return default_config
        
    def setup_logging(self):
        log_config = self.config.get("logging", {})
        
        log_file = log_config.get("file", "monitor.log")
        log_dir = os.path.dirname(os.path.abspath(log_file))
        if log_dir and not os.path.exists(log_dir):
            try:
                os.makedirs(log_dir, exist_ok=True)
            except Exception as e:
                print(f"Ошибка создания директории для логов: {e}")
                log_file = os.path.join(os.getcwd(), "monitor.log")
                print(f"Логи будут сохраняться в: {log_file}")
            
        log_level = getattr(logging, log_config.get("level", "INFO").upper())
        
        logging.getLogger().handlers.clear()
        
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        
        try:
            max_bytes = log_config.get("max_size_mb", 10) * 1024 * 1024
            backup_count = log_config.get("backup_count", 5)
            file_handler = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=max_bytes, backupCount=backup_count, encoding='utf-8'
            )
            file_handler.setFormatter(formatter)
            
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(formatter)
            
            telegram_error_handler = logging.FileHandler('telegram_errors.log', encoding='utf-8')
            telegram_error_handler.setLevel(logging.ERROR)
            telegram_error_handler.setFormatter(formatter)
            
            root_logger = logging.getLogger()
            root_logger.setLevel(log_level)
            root_logger.addHandler(file_handler)
            root_logger.addHandler(console_handler)
            root_logger.addHandler(telegram_error_handler)
        except Exception as e:
            print(f"Ошибка настройки логирования: {e}")
            logging.basicConfig(level=log_level, format='%(asctime)s - %(levelname)s - %(message)s')
        
    def format_uptime(self) -> str:
        uptime_seconds = time.time() - self.start_time
        days = int(uptime_seconds // 86400)
        hours = int((uptime_seconds % 86400) // 3600)
        minutes = int((uptime_seconds % 3600) // 60)
        
        if days > 0:
            return f"{days}д {hours}ч {minutes}м"
        elif hours > 0:
            return f"{hours}ч {minutes}м"
        else:
            return f"{minutes}м"
        
    def send_telegram_message(self, message: str) -> bool:
        telegram_config = self.config.get("notifications", {}).get("telegram", {})
        
        if not telegram_config.get("enabled", False):
            return False
            
        bot_token = telegram_config.get("bot_token")
        chat_id = telegram_config.get("chat_id")
        
        if not bot_token or not chat_id:
            return False
            
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            'chat_id': chat_id,
            'text': message,
            'parse_mode': 'HTML'
        }
        
        try:
            response = requests.post(url, json=payload, timeout=10)
            if response.status_code == 200:
                with self.lock:
                    self.stats["notifications_sent"] += 1
                return True
            else:
                logging.error(f"Ошибка отправки: {response.status_code} - {response.text}")
                return False
        except Exception as e:
            logging.error(f"Ошибка отправки сообщения: {e}")
            return False

    def can_send_alert(self, resource: str, level: AlertLevel) -> bool:
        cooldown = self.config.get("monitoring", {}).get("notification_cooldown_seconds", 300)
        current_time = time.time()
        
        key = f"{resource}_{level.value}"
        last_time = self.last_alert_times.get(key, 0)
        
        if current_time - last_time < cooldown:
            return False
            
        self.last_alert_times[key] = current_time
        return True
        
    def check_cpu(self) -> List[Alert]:
        config = self.config.get("monitoring", {}).get("resources", {}).get("cpu", {})
        if not config.get("enabled", True):
            return []
            
        threshold = config.get("threshold", 80)
        cpu_percent = psutil.cpu_percent(interval=1)
        
        with self.lock:
            self.current_metrics["cpu_percent"] = cpu_percent
        
        if cpu_percent > threshold:
            level = AlertLevel.CRITICAL if cpu_percent > 90 else AlertLevel.WARNING
            return [Alert(
                level=level,
                resource="CPU",
                message="Использование CPU превысило порог",
                value=cpu_percent,
                threshold=threshold,
                timestamp=datetime.now()
            )]
            
        return []
        
    def check_memory(self) -> List[Alert]:
        config = self.config.get("monitoring", {}).get("resources", {}).get("memory", {})
        if not config.get("enabled", True):
            return []
            
        threshold = config.get("threshold", 80)
        mem = psutil.virtual_memory()
        
        with self.lock:
            self.current_metrics["memory_percent"] = mem.percent
        
        if mem.percent > threshold:
            level = AlertLevel.CRITICAL if mem.percent > 90 else AlertLevel.WARNING
            return [Alert(
                level=level,
                resource="Память",
                message="Использование памяти превысило порог",
                value=mem.percent,
                threshold=threshold,
                timestamp=datetime.now()
            )]
            
        return []
        
    def check_disk(self) -> List[Alert]:
        config = self.config.get("monitoring", {}).get("resources", {}).get("disk", {})
        if not config.get("enabled", True):
            return []
            
        threshold = config.get("threshold", 80)
        disk_paths = config.get("paths", ["/"])
        alerts = []
        
        for partition in psutil.disk_partitions():
            if partition.mountpoint in disk_paths or not disk_paths:
                try:
                    disk = psutil.disk_usage(partition.mountpoint)
                    
                    if partition.mountpoint == "/":
                        with self.lock:
                            self.current_metrics["disk_percent"] = disk.percent
                            
                    if disk.percent > threshold:
                        level = AlertLevel.CRITICAL if disk.percent > 90 else AlertLevel.WARNING
                        alerts.append(Alert(
                            level=level,
                            resource=f"Диск {partition.mountpoint}",
                            message="Использование диска превысило порог",
                            value=disk.percent,
                            threshold=threshold,
                            timestamp=datetime.now()
                        ))
                except PermissionError:
                    logging.warning(f"Нет доступа к разделу диска: {partition.mountpoint}")
                except Exception as e:
                    logging.error(f"Ошибка проверки диска {partition.mountpoint}: {e}")
                    
        return alerts
        
    def check_network(self) -> List[Alert]:
        config = self.config.get("monitoring", {}).get("resources", {}).get("network", {})
        if not config.get("enabled", True):
            return []
            
        threshold_sent = config.get("threshold_sent_mbps", 10)
        threshold_recv = config.get("threshold_recv_mbps", 10)
        alerts = []
        
        current_net_io = psutil.net_io_counters()
        current_time = time.time()
        
        time_diff = current_time - self.prev_check_time
        if time_diff == 0:  
            time_diff = 1
            
        mb_sent = (current_net_io.bytes_sent - self.prev_net_io.bytes_sent) / (1024 * 1024)
        mb_recv = (current_net_io.bytes_recv - self.prev_net_io.bytes_recv) / (1024 * 1024)
        
        mb_sent_speed = mb_sent / time_diff
        mb_recv_speed = mb_recv / time_diff
        
        with self.lock:
            self.current_metrics["network_sent_speed"] = mb_sent_speed
            self.current_metrics["network_recv_speed"] = mb_recv_speed
        
        self.prev_net_io = current_net_io
        self.prev_check_time = current_time
        
        if mb_sent_speed > threshold_sent:
            alerts.append(Alert(
                level=AlertLevel.WARNING,
                resource="Сеть (исходящий трафик)",
                message="Скорость отправки превысила порог",
                value=mb_sent_speed,
                threshold=threshold_sent,
                timestamp=datetime.now()
            ))
            
        if mb_recv_speed > threshold_recv:
            alerts.append(Alert(
                level=AlertLevel.WARNING,
                resource="Сеть (входящий трафик)",
                message="Скорость получения превысила порог",
                value=mb_recv_speed,
                threshold=threshold_recv,
                timestamp=datetime.now()
            ))
            
        return alerts
        
    def get_system_status(self) -> str:
        cpu_percent = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        
        with self.lock:
            self.current_metrics["cpu_percent"] = cpu_percent
            self.current_metrics["memory_percent"] = mem.percent
            self.current_metrics["disk_percent"] = disk.percent
            
            mb_sent_speed = self.current_metrics["network_sent_speed"]
            mb_recv_speed = self.current_metrics["network_recv_speed"]
            
            status_message = (
                "📊 <b>Текущее состояние системы</b>\n\n"
                f"🖥️ <b>CPU:</b> {cpu_percent}%\n"
                f"🧠 <b>Память:</b> {mem.percent}%\n"
                f"💾 <b>Диск:</b> {disk.percent}%\n"
                f"📤 <b>Сеть (отправка):</b> {mb_sent_speed:.2f} MB/сек\n"
                f"📥 <b>Сеть (получение):</b> {mb_recv_speed:.2f} MB/сек\n\n"
                f"🔧 <b>Мониторинг:</b> {'✅ Включен' if self.monitoring_enabled else '❌ Выключен'}\n"
                f"⏰ <b>Время работы:</b> {self.format_uptime()}"
            )
            
            return status_message
        
    def get_stats_message(self) -> str:
        with self.lock:
            uptime = self.format_uptime()
            stats_message = (
                "📈 <b>Статистика мониторинга</b>\n\n"
                f"🔄 <b>Проверок:</b> {self.stats['checks']}\n"
                f"⚠️ <b>Оповещений:</b> {self.stats['alerts_triggered']}\n"
                f"📨 <b>Уведомлений отправлено:</b> {self.stats['notifications_sent']}\n"
                f"⏰ <b>Время работы:</b> {uptime}\n"
                f"📅 <b>Запущен:</b> {self.stats['start_time'].strftime('%Y-%m-%d %H:%M:%S')}"
            )
            
            return stats_message
        
    def get_alerts_message(self) -> str:
        with self.lock:
            if not self.alert_history:
                return "📝 <b>История оповещений</b>\n\nНет записей о оповещениях."
                
            alerts_text = "📝 <b>История оповещений</b>\n\n"
            for alert in self.alert_history[-10:]:  
                emoji = {
                    AlertLevel.INFO: "ℹ️",
                    AlertLevel.WARNING: "⚠️",
                    AlertLevel.CRITICAL: "🚨",
                    AlertLevel.ERROR: "❌"
                }.get(alert.level, "📊")
                
                alerts_text += (
                    f"{emoji} <b>{alert.resource}</b> - {alert.value} (порог: {alert.threshold})\n"
                    f"   <i>{alert.timestamp.strftime('%Y-%m-%d %H:%M:%S')}</i>\n\n"
                )
                
            return alerts_text
    
    def get_config_message(self) -> str:
        config = self.config.get("monitoring", {}).get("resources", {})
        config_message = (
            "⚙️ <b>Конфигурация мониторинга</b>\n\n"
            f"🖥️ <b>CPU порог:</b> {config.get('cpu', {}).get('threshold', 80)}%\n"
            f"🧠 <b>Память порог:</b> {config.get('memory', {}).get('threshold', 80)}%\n"
            f"💾 <b>Диск порог:</b> {config.get('disk', {}).get('threshold', 80)}%\n"
            f"📤 <b>Сеть отправка порог:</b> {config.get('network', {}).get('threshold_sent_mbps', 10)} MB/сек\n"
            f"📥 <b>Сеть получение порог:</b> {config.get('network', {}).get('threshold_recv_mbps', 10)} MB/сек\n"
        )
        
        return config_message
        
    def reload_config(self):
        try:
            with self.lock:
                old_config = self.config
                self.config = self.load_config(self.config_path)
                logging.info("Конфигурация перезагружена")
                return True
        except Exception as e:
            logging.error(f"Ошибка перезагрузки конфигурации: {e}")
            return False
        
    def save_state(self):
        state = {
            'alert_history': [alert.to_dict() for alert in self.alert_history],
            'stats': self.stats,
            'last_alert_times': self.last_alert_times
        }
        try:
            with open('monitor_state.json', 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            logging.info("Состояние сохранено")
        except Exception as e:
            logging.error(f"Ошибка сохранения состояния: {e}")
            
    def load_state(self):
        try:
            if os.path.exists('monitor_state.json'):
                with open('monitor_state.json', 'r', encoding='utf-8') as f:
                    state = json.load(f)
                
                self.alert_history = []
                for alert_data in state.get('alert_history', []):
                    self.alert_history.append(Alert(
                        level=AlertLevel[alert_data['level']],
                        resource=alert_data['resource'],
                        message=alert_data['message'],
                        value=alert_data['value'],
                        threshold=alert_data['threshold'],
                        timestamp=datetime.fromisoformat(alert_data['timestamp'])
                    ))
                
                self.stats = state.get('stats', self.stats)
                
                self.last_alert_times = state.get('last_alert_times', {})
                
                logging.info("Состояние загружено")
        except Exception as e:
            logging.error(f"Ошибка загрузки состояния: {e}")
        
    def check_resources(self):
        if not self.monitoring_enabled:
            return []
            
        with self.lock:
            self.stats["checks"] += 1
            
        alerts = []
        
        alerts.extend(self.check_cpu())
        alerts.extend(self.check_memory())
        alerts.extend(self.check_disk())
        alerts.extend(self.check_network())
        
        for alert in alerts:
            with self.lock:
                self.alert_history.append(alert)
                self.stats["alerts_triggered"] += 1
                
                if len(self.alert_history) > 100:
                    self.alert_history.pop(0)
                    
            if self.can_send_alert(alert.resource, alert.level):
                emoji = {
                    AlertLevel.INFO: "ℹ️",
                    AlertLevel.WARNING: "⚠️",
                    AlertLevel.CRITICAL: "🚨",
                    AlertLevel.ERROR: "❌"
                }.get(alert.level, "📊")
                
                message = (
                    f"{emoji} <b>{alert.resource} - {alert.level.value}</b>\n"
                    f"📊 Значение: {alert.value}\n"
                    f"📈 Порог: {alert.threshold}\n"
                    f"⏰ Время: {alert.timestamp.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"📝 {alert.message}"
                )
                
                self.send_telegram_message(message)
        
        if not alerts:
            logging.info("Все ресурсы в норме")
        else:
            logging.warning(f"Обнаружено {len(alerts)} проблем")
            
        return alerts
        
    def monitoring_loop(self):
        check_interval = self.config.get("monitoring", {}).get("check_interval_seconds", 60)
        
        logging.info(f"Запуск мониторинга с интервалом {check_interval} секунд")
        
        try:
            while not self.stop_event.is_set():
                self.check_resources()
                
                for _ in range(check_interval):
                    if self.stop_event.is_set():
                        break
                    time.sleep(1)
                    
        except Exception as e:
            logging.error(f"Ошибка в мониторинге: {e}")
            
            error_message = (
                "❌ <b>Критическая ошибка мониторинга</b>\n"
                f"Ошибка: {e}\n"
                f"Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            self.send_telegram_message(error_message)
    
    def start_monitoring(self):
        if self.monitoring_thread and self.monitoring_thread.is_alive():
            logging.warning("Мониторинг уже запущен")
            return
            
        self.stop_event.clear()
        self.monitoring_thread = threading.Thread(target=self.monitoring_loop, daemon=True)
        self.monitoring_thread.start()
        logging.info("Мониторинг запущен")
    
    def stop_monitoring(self):
        self.stop_event.set()
        if self.monitoring_thread and self.monitoring_thread.is_alive():
            self.monitoring_thread.join(timeout=10.0)
            logging.info("Мониторинг остановлен")
    
    def restart_monitoring(self):
        logging.info("Перезапуск мониторинга...")
        self.stop_monitoring()
        self.start_monitoring()
        return True
    
    def restart_application(self):
        logging.info("Полный перезапуск приложения...")
        self.save_state()
        python = sys.executable
        os.execl(python, python, *sys.argv)
        
    def run(self):
        self.start_monitoring()
        
        telegram_config = self.config.get("notifications", {}).get("telegram", {})
        if telegram_config.get("enabled", False) and TELEGRAM_AVAILABLE:
            self.telegram_thread = threading.Thread(target=self.run_telegram_bot, daemon=True)
            self.telegram_thread.start()
            logging.info("Telegram бот запущен в отдельном потоке")
        elif telegram_config.get("enabled", False) and not TELEGRAM_AVAILABLE:
            logging.error("Telegram бот включен в конфигурации, но библиотека python-telegram-bot не установлена")
        
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logging.info("Остановка приложения...")
        finally:
            self.stop_monitoring()
            self.save_state()
    
    def run_telegram_bot(self):
        if not TELEGRAM_AVAILABLE:
            logging.error("Библиотека python-telegram-bot не установлена. Установите: pip install python-telegram-bot")
            return
        
        telegram_config = self.config.get("notifications", {}).get("telegram", {})
        if not telegram_config.get("enabled", False):
            logging.info("Telegram бот отключен в конфигурации")
            return
            
        bot_token = telegram_config.get("bot_token")
        chat_id = telegram_config.get("chat_id")
        
        if not bot_token or not chat_id:
            logging.error("Не заданы TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID в конфигурации")
            return
        
        reply_keyboard = [
            ["📊 Статус", "📈 Статистика"],
            ["⚠️ Оповещения", "⚙️ Конфигурация"],
            ["✅ Включить", "❌ Выключить"],
            ["🔄 Перезапуск", "🔄 Полный перезапуск"],
            ["❓ Помощь"]
        ]
        reply_markup = ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True)
        
        async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
            welcome_text = (
                "🤖 <b>Мониторинг ресурсов системы</b>\n\n"
                "Доступные команды:\n"
                "/status - Текущее состояние системы\n"
                "/stats - Статистика мониторинга\n"
                "/alerts - История оповещений\n"
                "/config - Показать конфигурацию\n"
                "/enable - Включить мониторинг\n"
                "/disable - Выключить мониторинг\n"
                "/restart - Перезапустить мониторинг\n"
                "/full_restart - Полный перезапуск приложения\n"
                "/help - Показать справку\n\n"
                "Используйте кнопки ниже для быстрого доступа:"
            )
            
            keyboard = [
                [InlineKeyboardButton("📊 Статус", callback_data="status"),
                 InlineKeyboardButton("📈 Статистика", callback_data="stats")],
                [InlineKeyboardButton("⚠️ Оповещения", callback_data="alerts"),
                 InlineKeyboardButton("⚙️ Конфигурация", callback_data="config")],
                [InlineKeyboardButton("✅ Включить", callback_data="enable"),
                 InlineKeyboardButton("❌ Выключить", callback_data="disable")],
                [InlineKeyboardButton("🔄 Перезапуск", callback_data="restart"),
                 InlineKeyboardButton("🔄 Полный перезапуск", callback_data="full_restart")]
            ]
            
            inline_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                welcome_text, 
                parse_mode='HTML', 
                reply_markup=inline_markup
            )
            
            await update.message.reply_text(
                "Выберите действие:",
                reply_markup=reply_markup
            )
        
        async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
            help_text = (
                "🤖 <b>Справка по командам</b>\n\n"
                "📊 <b>Статус</b> - Текущее состояние системы\n"
                "📈 <b>Статистика</b> - Статистика мониторинга\n"
                "⚠️ <b>Оповещения</b> - История оповещений\n"
                "⚙️ <b>Конфигурация</b> - Показать конфигурацию\n"
                "✅ <b>Включить</b> - Включить мониторинг\n"
                "❌ <b>Выключить</b> - Выключить мониторинг\n"
                "🔄 <b>Перезапуск</b> - Перезапустить мониторинг\n"
                "🔄 <b>Полный перезапуск</b> - Полный перезапуск приложения\n"
                "❓ <b>Помощь</b> - Показать эту справку\n\n"
                "Используйте кнопки внизу экрана для быстрого доступа к командам."
            )
            
            await update.message.reply_text(help_text, parse_mode='HTML')
        
        async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
            status_msg = self.get_system_status()
            await update.message.reply_text(status_msg, parse_mode='HTML')
        
        async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
            stats_msg = self.get_stats_message()
            await update.message.reply_text(stats_msg, parse_mode='HTML')
        
        async def alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
            alerts_msg = self.get_alerts_message()
            await update.message.reply_text(alerts_msg, parse_mode='HTML')
        
        async def config_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
            config_msg = self.get_config_message()
            await update.message.reply_text(config_msg, parse_mode='HTML')
        
        async def enable(update: Update, context: ContextTypes.DEFAULT_TYPE):
            self.monitoring_enabled = True
            await update.message.reply_text("✅ Мониторинг включен")
        
        async def disable(update: Update, context: ContextTypes.DEFAULT_TYPE):
            self.monitoring_enabled = False
            await update.message.reply_text("❌ Мониторинг выключен")
        
        async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if self.restart_monitoring():
                await update.message.reply_text("✅ Мониторинг перезапущен")
            else:
                await update.message.reply_text("❌ Ошибка перезапуска мониторинга")
        
        async def full_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
            await update.message.reply_text("🔄 Полный перезапуск приложения...")
            threading.Thread(target=self.restart_application, daemon=True).start()
        
        async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
            query = update.callback_query
            await query.answer()
            
            if query.data == "status":
                status_msg = self.get_system_status()
                await query.edit_message_text(status_msg, parse_mode='HTML')
            elif query.data == "stats":
                stats_msg = self.get_stats_message()
                await query.edit_message_text(stats_msg, parse_mode='HTML')
            elif query.data == "alerts":
                alerts_msg = self.get_alerts_message()
                await query.edit_message_text(alerts_msg, parse_mode='HTML')
            elif query.data == "config":
                config_msg = self.get_config_message()
                await query.edit_message_text(config_msg, parse_mode='HTML')
            elif query.data == "enable":
                self.monitoring_enabled = True
                await query.edit_message_text("✅ Мониторинг включен")
            elif query.data == "disable":
                self.monitoring_enabled = False
                await query.edit_message_text("❌ Мониторинг выключен")
            elif query.data == "restart":
                if self.restart_monitoring():
                    await query.edit_message_text("✅ Мониторинг перезапущен")
                else:
                    await query.edit_message_text("❌ Ошибка перезапуска мониторинга")
            elif query.data == "full_restart":
                await query.edit_message_text("🔄 Полный перезапуск приложения...")
                threading.Thread(target=self.restart_application, daemon=True).start()
        
        async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
            text = update.message.text
            
            if text == "📊 Статус":
                await status(update, context)
            elif text == "📈 Статистика":
                await stats(update, context)
            elif text == "⚠️ Оповещения":
                await alerts(update, context)
            elif text == "⚙️ Конфигурация":
                await config_cmd(update, context)
            elif text == "✅ Включить":
                await enable(update, context)
            elif text == "❌ Выключить":
                await disable(update, context)
            elif text == "🔄 Перезапуск":
                await restart(update, context)
            elif text == "🔄 Полный перезапуск":
                await full_restart(update, context)
            elif text == "❓ Помощь":
                await help_command(update, context)
            else:
                await update.message.reply_text("Неизвестная команда. Используйте кнопки или /help для справки.")
        
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            application = Application.builder().token(bot_token).build()
            
            application.add_handler(CommandHandler("start", start))
            application.add_handler(CommandHandler("status", status))
            application.add_handler(CommandHandler("stats", stats))
            application.add_handler(CommandHandler("alerts", alerts))
            application.add_handler(CommandHandler("config", config_cmd))
            application.add_handler(CommandHandler("enable", enable))
            application.add_handler(CommandHandler("disable", disable))
            application.add_handler(CommandHandler("restart", restart))
            application.add_handler(CommandHandler("full_restart", full_restart))
            application.add_handler(CommandHandler("help", help_command))
            application.add_handler(CallbackQueryHandler(button_handler))
            application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
            
            logging.info("Запуск Telegram бота...")
            application.run_polling()
            
        except Exception as e:
            logging.error(f"Ошибка в работе Telegram бота: {e}")
        finally:
            if 'loop' in locals():
                loop.close()


def main():
    if DOTENV_AVAILABLE:
        load_dotenv()
    
    monitor = ResourceMonitor()
    
    monitor.run()


if __name__ == "__main__":
    main()
