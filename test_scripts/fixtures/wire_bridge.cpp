#include "wirecap.h"
#include <array>
#include <atomic>
#include <cstdint>
#include <thread>

int main() {
    std::array<int, 2> starts{-1, -1};
    std::thread first([&] { starts[0] = wire_start(); });
    std::thread second([&] { starts[1] = wire_start(); });
    first.join();
    second.join();
    if (starts[0] || starts[1] || !wire_ready()) return 1;
    if (wire_start()) return 2;

    std::array<std::thread, 4> senders;
    for (auto &sender : senders) sender = std::thread([] {
        for (int i = 0; i < 32; ++i) wire_emit_async("count", 0, nullptr, 0);
    });
    for (auto &sender : senders) sender.join();
    std::array<std::uint8_t, 8> output{};
    wire_event_t check{"check", 0, output.data(), output.size(), WIRE_SYNC, 0, 0};
    wire_emit(&check);
    if (!check.verdict || check.out_len != 8 || output[0] != 128) return 3;
    check.kind = "noop";
    wire_emit(&check);
    if (check.verdict || check.out_len) return 5;

    std::atomic<bool> stop{false};
    std::thread during_shutdown([&] {
        while (!stop) wire_emit_async("count", 0, nullptr, 0);
    });
    std::thread shutdown_first(wire_shutdown);
    std::thread shutdown_second(wire_shutdown);
    shutdown_first.join();
    shutdown_second.join();
    stop = true;
    during_shutdown.join();
    wire_shutdown();
    if (wire_ready() || wire_start() != -1) return 4;
    check.verdict = 1;
    wire_emit(&check);
    if (check.verdict || check.out_len) return 6;
}
