"""Ready-made niche templates.

A template is a questionnaire: the owner fills in facts about the business, and every answer whose
placeholders are all filled becomes a knowledge-base item. Nothing is invented for the owner:
an empty field produces no answer, so the bot never states a hours/price/policy nobody wrote.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    hint: str = ""
    long: bool = False


@dataclass(frozen=True)
class Item:
    title: str  # what a client would ask
    text: str  # the answer, with {field} placeholders


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    duration_min: int


@dataclass(frozen=True)
class Template:
    key: str
    title: str
    summary: str
    fields: tuple[Field, ...]
    items: tuple[Item, ...]
    services: tuple[ServiceSpec, ...] = ()
    greeting: str = ""


# ------------------------------------------------------------------ shared building blocks

F_HOURS = Field("hours", "Режим работы", "ежедневно с 10:00 до 22:00")
F_ADDRESS = Field("address", "Адрес", "г. Уфа, ул. Ленина, 15, 2 этаж")
F_PHONE = Field("phone", "Телефон", "+7 (347) 123-45-67")
F_BOOKING = Field("booking_how", "Как записаться", "Напишите нам в чате или позвоните — подберём время.", long=True)
F_PAYMENT = Field("payment", "Способы оплаты", "наличные, банковские карты, СБП")
F_PARKING = Field("parking", "Парковка", "Бесплатная парковка для гостей у входа.", long=True)
F_CANCEL = Field("cancel_policy", "Отмена и перенос", "Перенести или отменить можно не позже чем за 3 часа.", long=True)
F_SCHEDULE = Field(
    "schedule",
    "Расписание для онлайн-записи",
    "Пн-Пт 10:00-19:00, Сб 10:00-15:00 (по строке на группу дней). Оставьте пустым, если запись не нужна.",
    long=True,
)

COMMON_FIELDS = (F_HOURS, F_ADDRESS, F_PHONE, F_BOOKING, F_PAYMENT, F_PARKING, F_CANCEL)

COMMON_ITEMS = (
    Item("Режим работы", "Мы работаем {hours}."),
    Item("Адрес, как нас найти", "Наш адрес: {address}."),
    Item("Телефон для связи", "Телефон для связи: {phone}."),
    Item("Как записаться", "{booking_how}"),
    Item("Способы оплаты", "Принимаем к оплате: {payment}."),
    Item("Парковка", "{parking}"),
    Item("Отмена и перенос записи", "{cancel_policy}"),
)


def _t(key, title, summary, extra_fields, extra_items, services=(), greeting="") -> Template:
    return Template(
        key=key,
        title=title,
        summary=summary,
        fields=COMMON_FIELDS + extra_fields + (F_SCHEDULE,),
        items=extra_items + COMMON_ITEMS,
        services=services,
        greeting=greeting,
    )


TEMPLATES: tuple[Template, ...] = (
    _t(
        "universal",
        "Универсальный",
        "Подходит любому бизнесу: режим, адрес, цены, услуги, гарантии.",
        (
            Field("services_list", "Чем занимаетесь, какие услуги", "Ремонт и обслуживание бытовой техники", long=True),
            Field("price_list", "Цены", "Диагностика — 500 ₽, выезд мастера — 700 ₽", long=True),
            Field("warranty", "Гарантия", "Гарантия на работы — 6 месяцев.", long=True),
            Field("discounts", "Скидки и акции", "Скидка 10% при втором визите.", long=True),
        ),
        (
            Item("Какие услуги вы оказываете", "{services_list}"),
            Item("Цены и стоимость услуг", "{price_list}"),
            Item("Гарантия", "{warranty}"),
            Item("Скидки и акции", "{discounts}"),
        ),
        greeting="Здравствуйте! Подскажу по услугам и ценам и помогу записаться.",
    ),
    _t(
        "entertainment",
        "Картинг, аттракционы, квесты",
        "Заезды, возрастные ограничения, экипировка, детские праздники.",
        (
            Field("price_race", "Стоимость заезда", "Заезд 10 минут стоит 800 ₽, три заезда — 2 100 ₽."),
            Field("min_age", "Возраст", "Взрослая трасса — с 14 лет, детская — с 7 лет.", long=True),
            Field("height", "Ограничения по росту и весу", "Рост от 140 см, вес до 120 кг.", long=True),
            Field(
                "gear",
                "Что надеть и что выдаём",
                "Закрытая обувь и удобная одежда. Шлем и балаклаву выдаём бесплатно.",
                long=True,
            ),
            Field(
                "safety", "Безопасность", "Перед заездом проводим инструктаж, на трассе работают маршалы.", long=True
            ),
            Field(
                "groups",
                "Группы и корпоративы",
                "Для компаний от 8 человек — отдельные заезды и скидка. Нужна предварительная бронь.",
                long=True,
            ),
            Field(
                "birthday",
                "Дни рождения",
                "Организуем детские праздники: зал на 2 часа и заезды для именинника.",
                long=True,
            ),
        ),
        (
            Item("Сколько стоит заезд", "{price_race}"),
            Item("С какого возраста можно", "{min_age}"),
            Item("Ограничения по росту и весу", "{height}"),
            Item("Что надеть на заезд", "{gear}"),
            Item("Безопасность на трассе", "{safety}"),
            Item("Группы и корпоративы", "{groups}"),
            Item("Дни рождения и праздники", "{birthday}"),
        ),
        (ServiceSpec("Заезд", 30), ServiceSpec("Детский заезд", 30), ServiceSpec("Корпоративный заезд", 60)),
        "Здравствуйте! Расскажу про заезды, цены и свободное время — и запишу вас.",
    ),
    _t(
        "clinic",
        "Стоматология, клиника, ветеринар",
        "Первичный приём, цены, подготовка, дети, гарантия.",
        (
            Field(
                "first_visit", "Первый приём", "Первичная консультация бесплатная, длится около 30 минут.", long=True
            ),
            Field(
                "price_list",
                "Цены на основные услуги",
                "Осмотр — 500 ₽, чистка — 3 500 ₽, лечение кариеса — от 4 000 ₽",
                long=True,
            ),
            Field("documents", "Что взять с собой", "Паспорт и, если есть, снимки и выписки.", long=True),
            Field("kids", "Приём детей", "Принимаем детей с 3 лет, на приёме нужен один из родителей.", long=True),
            Field(
                "anesthesia", "Обезболивание", "Используем современные анестетики, лечение безболезненное.", long=True
            ),
            Field(
                "warranty", "Гарантия", "На лечение и протезирование даём гарантию — срок указан в договоре.", long=True
            ),
            Field(
                "urgent",
                "Острая боль, срочный приём",
                "При острой боли принимаем в день обращения — позвоните нам.",
                long=True,
            ),
        ),
        (
            Item("Первый приём, консультация", "{first_visit}"),
            Item("Цены на услуги", "{price_list}"),
            Item("Что взять с собой на приём", "{documents}"),
            Item("Принимаете ли детей", "{kids}"),
            Item("Больно ли лечить, обезболивание", "{anesthesia}"),
            Item("Гарантия на лечение", "{warranty}"),
            Item("Срочный приём, острая боль", "{urgent}"),
        ),
        (ServiceSpec("Консультация", 30), ServiceSpec("Профессиональная гигиена", 60), ServiceSpec("Лечение", 60)),
        "Здравствуйте! Подскажу по приёму и ценам и запишу к врачу.",
    ),
    _t(
        "autoservice",
        "Автосервис, детейлинг",
        "Диагностика, сроки, гарантия, запчасти, подменный автомобиль.",
        (
            Field(
                "price_list",
                "Цены на основные работы",
                "Диагностика ходовой — 1 000 ₽, замена масла — от 1 500 ₽",
                long=True,
            ),
            Field("diagnostic", "Диагностика", "Компьютерная диагностика занимает около часа.", long=True),
            Field(
                "terms",
                "Сроки ремонта",
                "Простые работы делаем в день обращения, сложные — по согласованию.",
                long=True,
            ),
            Field(
                "parts",
                "Запчасти",
                "Работаем с вашими запчастями и подбираем свои, гарантию даём на оба варианта.",
                long=True,
            ),
            Field("warranty", "Гарантия", "Гарантия на работы — 30 дней.", long=True),
            Field(
                "loaner",
                "Подменный автомобиль",
                "Подменный автомобиль предоставляем на время длительного ремонта.",
                long=True,
            ),
        ),
        (
            Item("Цены на работы", "{price_list}"),
            Item("Сколько занимает диагностика", "{diagnostic}"),
            Item("Сроки ремонта", "{terms}"),
            Item("Запчасти, свои или ваши", "{parts}"),
            Item("Гарантия на работы", "{warranty}"),
            Item("Подменный автомобиль", "{loaner}"),
        ),
        (ServiceSpec("Диагностика", 60), ServiceSpec("Замена масла", 60), ServiceSpec("Шиномонтаж", 60)),
        "Здравствуйте! Подскажу по ремонту и ценам и запишу на удобное время.",
    ),
    _t(
        "beauty",
        "Барбершоп, салон красоты",
        "Услуги и цены, мастера, опоздания, подарочные сертификаты.",
        (
            Field(
                "price_list",
                "Услуги и цены",
                "Мужская стрижка — 1 200 ₽, борода — 800 ₽, комплекс — 1 800 ₽",
                long=True,
            ),
            Field("masters", "Мастера", "У нас работают 4 мастера, можно выбрать по портфолио.", long=True),
            Field("late_policy", "Если опаздываю", "Ждём до 15 минут, потом запись может быть перенесена.", long=True),
            Field("first_visit", "Первый визит", "Новым гостям — консультация мастера бесплатно.", long=True),
            Field(
                "products",
                "Косметика и уход",
                "Продаём профессиональную косметику, мастер подскажет подходящую.",
                long=True,
            ),
            Field(
                "certificates", "Подарочные сертификаты", "Оформляем подарочные сертификаты на любую сумму.", long=True
            ),
        ),
        (
            Item("Услуги и цены", "{price_list}"),
            Item("Мастера, к кому записаться", "{masters}"),
            Item("Если я опаздываю", "{late_policy}"),
            Item("Первый визит", "{first_visit}"),
            Item("Косметика и уход", "{products}"),
            Item("Подарочные сертификаты", "{certificates}"),
        ),
        (ServiceSpec("Стрижка", 60), ServiceSpec("Борода", 30), ServiceSpec("Стрижка и борода", 90)),
        "Здравствуйте! Подскажу по услугам и запишу к мастеру.",
    ),
    _t(
        "food",
        "Кафе, доставка еды",
        "Зона и время доставки, минимальный заказ, меню, банкеты.",
        (
            Field("delivery_zone", "Зона доставки", "Доставляем по Уфе, кроме отдалённых посёлков.", long=True),
            Field("delivery_time", "Время доставки", "В среднем 45–60 минут.", long=True),
            Field(
                "delivery_price",
                "Стоимость доставки",
                "Доставка бесплатна при заказе от 1 500 ₽, иначе 150 ₽.",
                long=True,
            ),
            Field("min_order", "Минимальный заказ", "Минимальная сумма заказа — 800 ₽."),
            Field("menu", "Где посмотреть меню", "Меню и цены на сайте в разделе «Меню».", long=True),
            Field(
                "allergens",
                "Аллергены, особое питание",
                "В меню отмечены блюда без глютена и лактозы, состав уточняйте у администратора.",
                long=True,
            ),
            Field(
                "banquet",
                "Банкеты и бронь столов",
                "Принимаем банкеты до 40 человек, бронь стола — по телефону или в чате.",
                long=True,
            ),
        ),
        (
            Item("Куда доставляете", "{delivery_zone}"),
            Item("Как быстро привезёте", "{delivery_time}"),
            Item("Сколько стоит доставка", "{delivery_price}"),
            Item("Минимальный заказ", "{min_order}"),
            Item("Меню и цены", "{menu}"),
            Item("Аллергены и особое питание", "{allergens}"),
            Item("Банкеты и бронь стола", "{banquet}"),
        ),
        (ServiceSpec("Бронь стола", 120),),
        "Здравствуйте! Подскажу по меню, доставке и забронирую стол.",
    ),
    _t(
        "cleaning",
        "Клининг, ремонт, мастера на час",
        "Расчёт стоимости, что входит, химия, окно приезда.",
        (
            Field(
                "price_list",
                "Стоимость уборки",
                "Поддерживающая уборка — от 80 ₽/м², генеральная — от 150 ₽/м²",
                long=True,
            ),
            Field(
                "includes", "Что входит", "Влажная уборка, пылесос, кухня, санузлы, мытьё окон — по тарифу.", long=True
            ),
            Field("supplies", "Чья химия и инвентарь", "Привозим свою профессиональную химию и инвентарь.", long=True),
            Field("arrival", "Окно приезда", "Приезжаем в согласованное двухчасовое окно.", long=True),
            Field(
                "guarantee",
                "Гарантия качества",
                "Если что-то не устроило, переделаем бесплатно в течение 24 часов.",
                long=True,
            ),
            Field("areas", "Районы выезда", "Работаем по всему городу.", long=True),
        ),
        (
            Item("Сколько стоит уборка", "{price_list}"),
            Item("Что входит в уборку", "{includes}"),
            Item("Своя химия или ваша", "{supplies}"),
            Item("Во сколько приедете", "{arrival}"),
            Item("Гарантия качества", "{guarantee}"),
            Item("Районы выезда", "{areas}"),
        ),
        (ServiceSpec("Поддерживающая уборка", 180), ServiceSpec("Генеральная уборка", 300)),
        "Здравствуйте! Рассчитаю уборку и запишу на удобный день.",
    ),
    _t(
        "kids",
        "Детский центр, школа, кружки",
        "Расписание групп, пробное занятие, возраст, оплата.",
        (
            Field(
                "age_groups",
                "Возраст групп",
                "Принимаем детей от 4 до 14 лет, группы формируем по возрасту.",
                long=True,
            ),
            Field("trial", "Пробное занятие", "Первое пробное занятие бесплатное.", long=True),
            Field(
                "group_schedule",
                "Расписание групп",
                "Занятия по будням с 16:00 до 19:00, по субботам с 10:00 до 14:00.",
                long=True,
            ),
            Field("price_month", "Стоимость обучения", "Абонемент на месяц — 4 800 ₽ (8 занятий).", long=True),
            Field("bring", "Что взять с собой", "Сменная обувь и бутылка воды.", long=True),
            Field("teachers", "Преподаватели", "Занятия ведут педагоги с профильным образованием.", long=True),
        ),
        (
            Item("Возраст детей в группах", "{age_groups}"),
            Item("Пробное занятие", "{trial}"),
            Item("Расписание занятий", "{group_schedule}"),
            Item("Стоимость обучения", "{price_month}"),
            Item("Что взять на занятие", "{bring}"),
            Item("Кто преподаёт", "{teachers}"),
        ),
        (ServiceSpec("Пробное занятие", 60),),
        "Здравствуйте! Расскажу про группы и запишу на пробное занятие.",
    ),
)

BY_KEY = {t.key: t for t in TEMPLATES}


def get_template(key: str) -> Template | None:
    return BY_KEY.get(key)


def placeholders(text: str) -> set[str]:
    return set(_PLACEHOLDER.findall(text))


def render(text: str, values: dict[str, str]) -> str:
    # regex substitution (not str.format): owner text may contain braces
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), ""), text)


def build_items(tpl: Template, values: dict[str, str]) -> list[tuple[str, str]]:
    """(title, text) for every item whose placeholders are all filled."""
    clean = {k: " ".join(v.split()) for k, v in values.items() if v and v.strip()}
    out = []
    for it in tpl.items:
        need = placeholders(it.text)
        if need and not need <= clean.keys():
            continue
        out.append((it.title, render(it.text, clean).strip()))
    return out
