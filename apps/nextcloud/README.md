# Nextcloud

Menhir deploys Nextcloud with PostgreSQL and Redis. The files share and all
application/database volumes are included in the backup contract. Backups enter
maintenance mode and create a PostgreSQL dump before the Btrfs snapshot.

