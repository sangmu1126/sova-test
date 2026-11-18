"""Private stdin configuration; safe metadata only; parent always bounds lifetime."""

import json
import sys
import time

from qualification_pool import ControlPool


def emit(**value):
    print(json.dumps(value), flush=True)


def command():
    if json.loads(sys.stdin.readline()) != 'go':
        raise ValueError('Unexpected qualification command')


def main():
    config = json.loads(sys.stdin.readline())
    if set(config) != {'dsn', 'role', 'stage'} or config['stage'] not in ('checkout', 'reset'):
        raise ValueError('Unexpected qualification configuration')
    with ControlPool(config['dsn'], config['role'], max_size=1) as pool:
        with pool.connect() as conn:
            pid = conn.info.backend_pid
            assert conn.execute('SELECT 1').fetchone() == (1,)
        until = time.monotonic()+3
        while pool.pool.get_stats()['pool_available'] != 1:
            if time.monotonic() >= until:
                raise RuntimeError('Initial session reset did not complete')
            time.sleep(.01)
        if config['stage'] == 'checkout':
            emit(event='ready', pid=pid)
            command()
            emit(event='started')
        else:
            emit(event='ready', pid=pid)
            command()
            with pool.connect() as conn:
                emit(event='borrowed', pid=conn.info.backend_pid)
                command()
            emit(event='returned')
        started = time.monotonic()
        try:
            with pool.connect() as conn:
                assert conn.execute('SELECT 1').fetchone() == (1,)
            emit(event='acquired', elapsed_ms=round((time.monotonic()-started)*1000, 3))
        except Exception as error:
            emit(event='rejected', error_class=type(error).__name__,
                 elapsed_ms=round((time.monotonic()-started)*1000, 3))
        command()  # Parent heals the relay before normal pool shutdown.
    emit(event='closed')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        emit(event='failed', error_class=type(error).__name__)
        sys.exit(1)
