from knightbus.scheduler import poll_realtime_active_hours, refresh_features, refresh_static


def main() -> None:
    refresh_static()
    poll_realtime_active_hours()
    refresh_features()


if __name__ == "__main__":
    main()

