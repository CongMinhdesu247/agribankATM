from agribank_recon import config
from agribank_recon.reconcile_engine import initialize_database


def main() -> None:
    initialize_database()
    print(f"Database initialized ({config.DATABASE_KIND}).")


if __name__ == "__main__":
    main()

