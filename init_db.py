from config import DB_PATH
from reconcile_engine import initialize_database


def main() -> None:
    initialize_database()
    print(f"Database initialized: {DB_PATH}")


if __name__ == "__main__":
    main()

