"""SDK 无 GUI 用法演示：python examples/sdk_demo.py [midi输入端口名]"""

import sys
import time

import api


def main() -> None:
    app = api.create_app()

    @api.on("midi.message")
    def on_msg(parsed):
        print(f"[{parsed.type}] ch={parsed.channel} values={parsed.values} hex={parsed.raw_hex}")

    @api.on("midi.signal")
    def on_sig(payload):
        print(">> 信号:", payload["signals"])

    port = sys.argv[1] if len(sys.argv) > 1 else None
    if port:
        app.open_input(port)
        app.start()
        print(f"监听 {port}，Ctrl+C 退出")
        try:
            while True:
                time.sleep(0.1)
        except KeyboardInterrupt:
            app.stop()
    else:
        print("用法: python examples/sdk_demo.py <MIDI输入端口名>")
        print("可用输入端口:", app.engine.list_inputs())


if __name__ == "__main__":
    main()