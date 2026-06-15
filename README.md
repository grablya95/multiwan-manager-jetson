# Multi-WAN Manager для Jatson

Production-орієнтований сервіс автоматичного перемикання між кількома WAN-інтерфейсами на NVIDIA Jatson/Linux.

Підтримується штатний Python 3.6 з Ubuntu 18.04 та старих Jetson images. Для нього зафіксовано сумісні Flask 2.0.3, Waitress 2.0.0 і backport `dataclasses`.

Інсталятор примусово оновлює інструменти Python 3.6 до `pip 21.3.1`, `setuptools 58.5.3` і `wheel 0.37.1`. Це потрібно, щоб старий системний pip правильно розпізнавав ARM64 wheels і не намагався без потреби збирати MarkupSafe з вихідного коду.

## Швидке встановлення

```bash
chmod +x install.sh
sudo ./install.sh
```

Інсталятор:

- встановлює Python, `iproute2`, `ping` і `conntrack`;
- створює окреме virtual environment;
- встановлює Flask і production WSGI-сервер Waitress;
- копіює програму в `/opt/multiwan-manager`;
- створює та запускає `multiwan-manager.service`;
- вмикає автоматичний запуск після перезавантаження.

Панель після встановлення:

```text
http://IP_СЕРВЕРА:5000
```

## Керування сервісом

```bash
sudo systemctl status multiwan-manager --no-pager
sudo systemctl restart multiwan-manager
sudo systemctl stop multiwan-manager
sudo journalctl -u multiwan-manager -n 50 --no-pager
```

Параметри веб-сервера знаходяться у `/etc/default/multiwan-manager`:

```ini
WAN_BIND=0.0.0.0
WAN_PORT=5000
WAN_THREADS=4
LOG_LEVEL=INFO
```

Після зміни параметрів:

```bash
sudo systemctl restart multiwan-manager
```

## Маршрутизація

Усі доступні WAN мають default route:

- активний WAN отримує metric `10`;
- резервні WAN отримують metric від `500`;
- окремі `/32` routes прив'язують health-check до потрібного інтерфейсу;
- маршрути змінюються лише тоді, коли поточна таблиця не відповідає потрібному стану.

Це прибирає опцію `Default тільки активному` та зменшує кількість перебудов route table.

Після реального перемикання за бажанням виконується `conntrack -F`, щоб нові з'єднання одразу пішли через актуальний WAN. Tailscale, ZeroTier та інші VPN у коді не згадуються і не перезапускаються.

## Оптимізації

- WAN перевіряються паралельно.
- Для одного WAN не створюється зайвий thread pool.
- Дані link state та IPv4 читаються одним викликом `ip -j -4 addr`.
- Захист від одночасного запуску кількох monitor cycles.
- Tracking routes перевіряються за плановим аудитом, а не переписуються кожен цикл.
- Default routes не змінюються, якщо метрики вже правильні.
- У лог пишуться перемикання, зміни стану та помилки, а не кожна перевірка.
- `systemd` обмежує пам'ять, кількість процесів і частоту повідомлень журналу.
- Веб-панель опитує API рідше у фоновій вкладці й оновлює тільки змінені показники.
- Налаштування зберігаються в `/opt/multiwan-manager/providers.json`.
- Зберігаються також Auto/фіксований режим і вибраний фіксований WAN.

## Виявлення інтерфейсів

Профіль WAN прив'язується до MAC-адреси, а якщо MAC недоступна — до системної назви інтерфейсу. Через `udevadm` і sysfs панель визначає тип адаптера: Ethernet, USB Ethernet, LTE/WWAN або Wi-Fi, а також показує модель, драйвер і системне ім'я.

Фізично відсутній інтерфейс прибирається з панелі, але його профіль залишається в `providers.json`. Після повернення адаптера його власна назва, пріоритет, gateway і track hosts відновлюються без дубліката.

## Оновлення

Запустіть новий `install.sh` з каталогу оновленої версії:

```bash
sudo ./install.sh
```

Інсталятор зупинить сервіс, оновить файли й знову його запустить. Існуючий `providers.json` не видаляється.

## Видалення

```bash
chmod +x uninstall.sh
sudo ./uninstall.sh
```

Скрипт видаляє сервіс, але залишає `/opt/multiwan-manager` і конфіг для безпечного відновлення.

## Ручний запуск

Для тестування без `systemd`:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
sudo .venv/bin/python app.py
```

Застосунок запускається через Waitress, тому попередження Flask development server більше не з'являється.
