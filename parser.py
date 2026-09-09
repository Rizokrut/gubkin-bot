"""
Модуль парсинга сайта lk.gubkin.uz.

ЧЕСТНОЕ ПРЕДУПРЕЖДЕНИЕ: я не могу открыть lk.gubkin.uz сам (страница расписания
защищена капчей и сессией, у меня нет доступа к вашему кабинету), поэтому я не
знаю ТОЧНЫЕ названия CSS-классов, id и структуру HTML-таблицы расписания на
этом конкретном сайте. Ниже — рабочий каркас с типичной структурой для таких
студенческих порталов (выпадающие списки <select> для курса/факультета/группы
и таблица <table> с расписанием). Его нужно донастроить под реальный HTML —
это буквально замена нескольких строк с CSS-селекторами, а не переписывание
всего файла.

КАК ДОНАСТРОИТЬ (шаги без опыта программирования, прямо с телефона):
1. Откройте lk.gubkin.uz в браузере Chrome на телефоне, войдите, пройдите капчу,
   откройте страницу расписания какой-нибудь группы.
2. В адресной строке Chrome введите: view-source:https://lk.gubkin.uz/адрес_страницы
   (это покажет исходный HTML-код страницы).
3. Найдите (Ctrl+F / поиск на странице) слово "select" — это выпадающие списки
   курса/факультета/группы, и слово "table" — это таблица с расписанием.
4. Скопируйте фрагмент HTML вокруг этих мест (достаточно 20-30 строк) и пришлите
   его мне в чат — я перепишу CSS-селекторы под реальную структуру за одно
   сообщение, остальной код менять не придётся.

Если у сайта вместо обычных <select>/<table> используется JavaScript-фреймворк
(данные подгружаются через API, а не отдаются сразу в HTML) — это тоже видно
по исходному коду (в HTML почти пусто), и в этом случае лучше найти сетевые
запросы через "Инструменты разработчика" (или прислать мне ссылку на страницу
расписания — я попробую посмотреть, что отдаёт сервер).
"""

import httpx
from bs4 import BeautifulSoup

from config import BASE_URL

WEEKDAY_MAP_RU = {
    "понедельник": 0,
    "вторник": 1,
    "среда": 2,
    "четверг": 3,
    "пятница": 4,
    "суббота": 5,
    "воскресенье": 6,
}


def _client(cookie: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=BASE_URL,
        cookies={"PHPSESSID": cookie},
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        },
        timeout=30.0,
        follow_redirects=True,
    )


async def fetch_structure(cookie: str, schedule_path: str = "/schedule") -> list[tuple[str, str, str]]:
    """
    Скачивает страницу расписания и достаёт список (курс, факультет, группа)
    из выпадающих списков. ТРЕБУЕТ донастройки под реальный HTML (см. докстринг
    файла выше) — сейчас предполагается, что на странице есть:
      <select name="course">...<option value="1">I курс</option>...</select>
      <select name="faculty">...<option value="...">...</option>...</select>
      <select name="group">...<option value="УРБ-25-05">УРБ-25-05</option>...</select>
    и что варианты факультета/группы можно достать одним общим списком
    (если сайт подгружает их динамически через AJAX при выборе курса —
    напишите мне, это отдельная логика с несколькими запросами).
    """
    async with _client(cookie) as client:
        response = await client.get(schedule_path)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        courses = {
            opt.get("value", opt.text.strip()): opt.text.strip()
            for opt in soup.select("select[name='course'] option")
        }
        faculties = [opt.text.strip() for opt in soup.select("select[name='faculty'] option")]
        groups = [opt.text.strip() for opt in soup.select("select[name='group'] option")]

        # ЗАГЛУШКА: без реальной структуры сайта невозможно правильно связать
        # курс -> факультет -> группу между собой. Пока просто кладём всё в
        # один "виртуальный" курс/факультет, чтобы бот в принципе работал и
        # показывал список групп для выбора. После того как пришлёте реальный
        # HTML, я перепишу эту функцию так, чтобы связи были верными.
        rows: list[tuple[str, str, str]] = []
        for group in groups:
            if not group:
                continue
            rows.append(("Все курсы", "Все факультеты", group))
        return rows


async def fetch_schedule(cookie: str, group_name: str, schedule_path: str = "/schedule") -> list[dict]:
    """
    Скачивает расписание для конкретной группы и возвращает список пар вида:
    {"weekday": 0, "time_slot": "08:30-09:50", "subject": "...", "room": "...",
     "teacher": "...", "lesson_type": "лекция", "week_parity": "all"}

    ТРЕБУЕТ донастройки под реальный HTML — сейчас предполагается таблица вида:
    <table class="schedule-table">
      <tr><td>Понедельник</td><td>08:30-09:50</td><td>Предмет</td><td>Ауд. 305</td><td>Иванов И.И.</td></tr>
      ...
    </table>
    """
    async with _client(cookie) as client:
        response = await client.get(schedule_path, params={"group": group_name})
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        lessons: list[dict] = []
        table = soup.select_one("table.schedule-table")
        if not table:
            return lessons

        current_weekday = None
        for row in table.select("tr"):
            cells = [c.get_text(strip=True) for c in row.select("td")]
            if not cells:
                continue

            first_cell_lower = cells[0].lower()
            if first_cell_lower in WEEKDAY_MAP_RU:
                current_weekday = WEEKDAY_MAP_RU[first_cell_lower]
                cells = cells[1:]

            if current_weekday is None or len(cells) < 3:
                continue

            time_slot = cells[0] if len(cells) > 0 else ""
            subject = cells[1] if len(cells) > 1 else ""
            room = cells[2] if len(cells) > 2 else ""
            teacher = cells[3] if len(cells) > 3 else ""

            lessons.append(
                {
                    "weekday": current_weekday,
                    "time_slot": time_slot,
                    "subject": subject,
                    "room": room,
                    "teacher": teacher,
                    "lesson_type": "",
                    "week_parity": "all",
                }
            )

        return lessons


async def check_cookie_valid(cookie: str, schedule_path: str = "/schedule") -> bool:
    """Простая проверка: сервер не перекинул нас обратно на страницу входа/капчи."""
    async with _client(cookie) as client:
        response = await client.get(schedule_path)
        if response.status_code >= 400:
            return False
        lowered = response.text.lower()
        if "captcha" in lowered or "капча" in lowered or "вход" in lowered and "пароль" in lowered:
            return False
        return True
