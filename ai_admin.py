"""
ИИ-админ для Minecraft (Paper) сервера.

Что делает:
1. Следит за logs/latest.log сервера в реальном времени.
2. Распознаёт сообщения чата и события join/leave.
3. Отправляет их в локальную модель (Ollama) с инструкцией -
   решить, нарушено ли правило, и что делать.
4. В зависимости от тяжести нарушения - шлёт предупреждение в чат,
   мьютит (через EssentialsX /mute), кикает или банит игрока через RCON.

Запуск:
    pip install mcrcon requests pyyaml
    python ai_admin.py

Перед запуском скопируй config.example.yaml в config.yaml и заполни.
"""

import re
import time
import json
import requests
from pathlib import Path
from mcrcon import MCRcon

try:
    import yaml
except ImportError:
    yaml = None

# ============== НАСТРОЙКИ ==============
# Все секретные/личные настройки (пароль RCON, путь к логу) лежат
# в отдельном файле config.yaml, который НЕ загружается на GitHub
# (он указан в .gitignore). Смотри config.example.yaml как образец.

def load_config() -> dict:
    config_path = Path(__file__).parent / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(
            "Не найден config.yaml. Скопируй config.example.yaml в config.yaml "
            "и заполни своими значениями (путь к логу, пароль RCON)."
        )
    if yaml is None:
        raise ImportError("Нужен пакет pyyaml: pip install pyyaml")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


CONFIG = load_config()
# ===================================================================

# Сколько раз игрок должен нарушить правила прежде чем будет
# применена более жёсткая мера. Хранится только в памяти процесса
# (сбрасывается при перезапуске скрипта).
violation_counts: dict[str, int] = {}

# Длительность мьюта по умолчанию (EssentialsX формат, напр. "10m", "1h")
MUTE_DURATION = "10m"

# Системные правила сервера - редактируй под себя
SERVER_RULES = """
Правила сервера:
1. Запрещён мат и оскорбления других игроков.
2. Запрещён спам (повторение одного сообщения много раз).
3. Запрещена реклама других серверов/проектов.
4. Запрещены угрозы и токсичное поведение.
5. Обычное общение, шутки, игровые обсуждения - это нормально, не нарушение.
"""

# Регулярки для парсинга строк лога Paper/Spigot
CHAT_RE = re.compile(r"\[.*?\] \[Server thread/INFO\]: <(?P<player>[^>]+)> (?P<message>.*)")
JOIN_RE = re.compile(r"\[.*?\] \[Server thread/INFO\]: (?P<player>\w+) joined the game")
LEAVE_RE = re.compile(r"\[.*?\] \[Server thread/INFO\]: (?P<player>\w+) left the game")


def ask_ollama(player: str, message: str, prior_violations: int) -> dict:
    """Спрашивает локальную модель, нарушает ли сообщение правила и что делать."""
    prompt = f"""Ты - ИИ-модератор Minecraft-сервера. Вот правила:
{SERVER_RULES}

Игрок "{player}" написал в чат: "{message}"
У этого игрока уже было зафиксировано нарушений ранее в этой сессии: {prior_violations}.

Оцени сообщение и ответь СТРОГО в формате JSON, без пояснений:
{{"violation": true/false, "severity": "none/low/medium/high/severe", "action": "ignore/warn_chat/warn_private/mute/kick/ban", "reply": "текст для игрока/чата, если нужен, иначе пустая строка"}}

Критерии выбора action (учитывай prior_violations - повторные нарушения наказываются строже):
- Обычное сообщение (приветствие, игровой вопрос, шутка) -> violation false, action "ignore".
- Лёгкая грубость без явного мата, первый раз -> severity "low", action "warn_private".
- Явный мат / оскорбление другого игрока / спам, первый-второй раз -> severity "medium", action "warn_chat".
- Повтор нарушения после предупреждений (prior_violations >= 2) или явный спам флудом -> severity "high", action "mute".
- Угрозы, травля, систематическая токсичность, попытки обойти мьют -> severity "high", action "kick".
- Только для самых тяжёлых случаев: реклама вредоносных ссылок, разжигание ненависти, угрозы насилием, попытки читерства через чат-команды (например выдать себе права) -> severity "severe", action "ban".
- НЕ используй ban и kick за обычную грубость или единичный мат - это для систематических/опасных случаев.
- reply - короткий, дружелюбный, но твёрдый текст на русском, 1 предложение.
"""

    try:
        resp = requests.post(
            CONFIG["ollama_url"],
            json={
                "model": CONFIG["ollama_model"],
                "prompt": prompt,
                "stream": False,
                "format": "json",
            },
            timeout=30,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "{}")
        return json.loads(raw)
    except Exception as e:
        print(f"[ИИ-админ] Ошибка запроса к Ollama: {e}")
        return {"violation": False, "action": "ignore", "reply": ""}


def send_rcon_command(command: str):
    """Отправляет команду на сервер через RCON."""
    try:
        with MCRcon(CONFIG["rcon_host"], CONFIG["rcon_password"], port=CONFIG["rcon_port"]) as mcr:
            response = mcr.command(command)
            print(f"[RCON] -> {command}  |  ответ: {response}")
    except Exception as e:
        print(f"[ИИ-админ] Ошибка RCON: {e}")


def handle_chat_message(player: str, message: str):
    print(f"[ЧАТ] <{player}> {message}")

    prior = violation_counts.get(player, 0)
    result = ask_ollama(player, message, prior)
    action = result.get("action", "ignore")
    reply = result.get("reply", "").strip()
    is_violation = result.get("violation", False)

    if action == "ignore" or not is_violation:
        return

    # Считаем нарушение и решаем, что делать
    violation_counts[player] = prior + 1

    if action == "warn_chat" and reply:
        send_rcon_command(f'say [ИИ-админ] {reply}')
    elif action == "warn_private" and reply:
        send_rcon_command(f'tell {player} [ИИ-админ] {reply}')
    elif action == "mute":
        # Команда EssentialsX. Если у тебя другой плагин для мьюта,
        # поменяй формат команды здесь.
        send_rcon_command(f'mute {player} {MUTE_DURATION}')
        if reply:
            send_rcon_command(f'say [ИИ-админ] {player} замьючен на {MUTE_DURATION}: {reply}')
    elif action == "kick":
        reason = reply or "Нарушение правил сервера"
        send_rcon_command(f'kick {player} {reason}')
    elif action == "ban":
        reason = reply or "Грубое нарушение правил сервера"
        send_rcon_command(f'ban {player} {reason}')

    print(f"[РЕШЕНИЕ] игрок={player} violation={is_violation} severity={result.get('severity')} "
          f"action={action} всего_нарушений={violation_counts[player]}")


def handle_join(player: str):
    print(f"[JOIN] {player} зашёл на сервер")
    # Можно сделать приветствие через ИИ, но для старта - просто лог.


def handle_leave(player: str):
    print(f"[LEAVE] {player} вышел")
    violation_counts.pop(player, None)


def tail_log(path: str, poll_interval: float):
    """Читает лог-файл с конца, выдавая новые строки по мере их появления."""
    p = Path(path)
    while not p.exists():
        print(f"[ИИ-админ] Жду появления файла лога: {path}")
        time.sleep(2)

    with open(p, "r", encoding="utf-8", errors="ignore") as f:
        f.seek(0, 2)  # перейти в конец файла - не обрабатываем старые строки
        while True:
            line = f.readline()
            if not line:
                time.sleep(poll_interval)
                continue
            yield line.rstrip("\n")


def main():
    print("=== ИИ-админ запущен ===")
    print(f"Слежу за логом: {CONFIG['log_path']}")
    print(f"Модель: {CONFIG['ollama_model']} (Ollama)")
    print("Жду события...\n")

    for line in tail_log(CONFIG["log_path"], CONFIG["poll_interval"]):
        chat_match = CHAT_RE.search(line)
        if chat_match:
            handle_chat_message(chat_match.group("player"), chat_match.group("message"))
            continue

        join_match = JOIN_RE.search(line)
        if join_match:
            handle_join(join_match.group("player"))
            continue

        leave_match = LEAVE_RE.search(line)
        if leave_match:
            handle_leave(leave_match.group("player"))
            continue


if __name__ == "__main__":
    main()
