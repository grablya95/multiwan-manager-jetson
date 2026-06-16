# Multi-WAN Manager для Jetson
<img width="754" height="513" alt="pic-1" src="https://github.com/user-attachments/assets/4b70d041-cce9-47c7-b4fd-da8b642ef045" />
Веб-панель і systemd-сервіс для автоматичного перемикання між кількома WAN-інтерфейсами на NVIDIA Jetson / Linux.

Сервіс перевіряє якість кожного інтернет-каналу, вибирає активний WAN, змінює default route і за потреби скидає старі з'єднання через `conntrack`.

## Підтримка

- Ubuntu 18.04 або новіше
- NVIDIA Jetson зі старими JetPack images
- Python 3.6 або новіший
- NetworkManager не обов'язковий
- Права root потрібні для зміни маршрутів

## Встановлення

```bash
git clone https://github.com/grablya95/multiwan-manager-jetson.git
cd multiwan-manager-jetson
chmod +x install.sh
sudo ./install.sh
```

Після встановлення панель буде доступна:

```text
http://IP_СЕРВЕРА:5000
```

## Що встановлює install.sh

Системні пакети:

- `python3`
- `python3-venv`
- `python3-dev` або `python3-devel`
- `build-essential` або `gcc`
- `iproute2` / `iproute`
- `iputils-ping` / `iputils`
- `conntrack` / `conntrack-tools`

Python-пакети:

- `Flask==2.0.3`
- `Werkzeug==2.0.3`
- `Jinja2==3.0.3`
- `MarkupSafe==2.0.1`
- `itsdangerous==2.0.1`
- `click==8.0.4`
- `waitress==2.0.0`
- `dataclasses==0.8` для Python 3.6

Для старих систем інсталятор оновлює інструменти Python до:

- `pip==21.3.1`
- `setuptools==58.5.3`
- `wheel==0.37.1`

## Що створюється в системі

```text
/opt/multiwan-manager/
/opt/multiwan-manager/app.py
/opt/multiwan-manager/templates/index.html
/opt/multiwan-manager/requirements.txt
/opt/multiwan-manager/providers.json
/opt/multiwan-manager/.venv/
/etc/systemd/system/multiwan-manager.service
/etc/default/multiwan-manager
```

`providers.json` створюється автоматично та зберігає налаштування веб-панелі.

## Керування сервісом

```bash
sudo systemctl status multiwan-manager --no-pager
sudo systemctl restart multiwan-manager
sudo systemctl stop multiwan-manager
sudo journalctl -u multiwan-manager -n 50 --no-pager
```

Автозапуск увімкнений після встановлення:

```bash
sudo systemctl enable multiwan-manager
```

## Налаштування веб-сервера

Файл:

```text
/etc/default/multiwan-manager
```

Параметри:

```ini
WAN_BIND=0.0.0.0
WAN_PORT=5000
WAN_THREADS=4
LOG_LEVEL=INFO
```

Після зміни:

```bash
sudo systemctl restart multiwan-manager
```

## Як працює failover

1. Сервіс знаходить WAN-інтерфейси через таблицю default routes.
2. Для кожного інтерфейсу створюється профіль.
3. Профіль прив'язується до MAC-адреси, а якщо MAC немає - до назви інтерфейсу.
4. Для health-check додаються окремі `/32` маршрути до track hosts.
5. Кожен WAN перевіряється через `ping -I interface`.
6. Найкращий доступний WAN отримує default route з metric `10`.
7. Резервні WAN отримують default route з metric від `500`.
8. Якщо активний WAN падає або деградує, сервіс перемикає маршрут на інший WAN.
9. Якщо увімкнено `Очищення conntrack`, старі сесії скидаються командою `conntrack -F`.

## Функції веб-панелі

- автоматичний режим failover;
- фіксація конкретного WAN вручну;
- вибір пріоритету для кожного WAN;
- налаштування ping/loss порогів;
- налаштування track hosts;
- показ активного WAN;
- показ ping, jitter, packet loss;
- показ Ethernet, USB Ethernet, LTE/WWAN або Wi-Fi;
- показ системного імені інтерфейсу, драйвера та моделі;
- збереження налаштувань після перезапуску.

## Перемикачі в панелі

`Аварійний fallback`  
Повертає ручний режим у `Auto`, якщо зафіксований WAN упав.

`Реакція на деградацію`  
Перемикає WAN не тільки при повному падінні, а й при високому ping або packet loss.

`Очищення conntrack`  
Скидає старі з'єднання після зміни WAN, щоб нові сесії одразу йшли через активний провайдер.

## Що зберігається

У файлі:

```text
/opt/multiwan-manager/providers.json
```

Зберігається:

- назва WAN;
- пріоритет;
- gateway;
- track hosts;
- ping/loss пороги;
- Auto або фіксований режим;
- вибраний фіксований WAN;
- глобальні налаштування failover.

## Оновлення

```bash
cd multiwan-manager-jetson
git pull
sudo ./install.sh
```

Інсталятор оновлює файли програми, але не видаляє `providers.json`.

## Видалення сервісу

```bash
chmod +x uninstall.sh
sudo ./uninstall.sh
```

Це видаляє systemd-сервіс, але залишає `/opt/multiwan-manager`.

Повне видалення:

```bash
sudo systemctl disable --now multiwan-manager.service
sudo rm -f /etc/systemd/system/multiwan-manager.service
sudo rm -f /etc/default/multiwan-manager
sudo rm -rf /opt/multiwan-manager
sudo systemctl daemon-reload
sudo systemctl reset-failed
```

## Ручний запуск

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
sudo .venv/bin/python app.py
```

У production-режимі застосунок запускається через Waitress, а не через Flask development server.
