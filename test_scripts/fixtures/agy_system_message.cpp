/* Exercise the real system-message handler against readable and invalid Go-shaped objects. */
#include "gocall.h"
#include "wirecap.h"
#include <array>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#define CHECK(expr) do { if (!(expr)) { \
    std::fprintf(stderr, "%s:%d: %s\n", __FILE__, __LINE__, #expr); \
    std::exit(1); \
} } while (0)

static std::vector<uint8_t> packet;
static std::string kind;
static uint64_t stream;
static int emitted;

extern "C" void wire_emit(wire_event_t *event)
{
    CHECK(event->mode == WIRE_ASYNC);
    kind = event->kind;
    stream = event->stream_id;
    packet.assign(event->data, event->data + event->len);
    emitted++;
}
extern "C" void wire_shutdown(void) {}

int main(int argc, char **argv)
{
    CHECK(argc == 3);
    const std::string mode = argv[1];
    std::string text = "Harness notification: caf\xc3\xa9 \xe4\xb8\xad\xe6\x96\x87";
    std::string event = "agent_message";
    if (mode == "nul") text = std::string("before\0after", 12);
    if (mode == "long") text.assign(2u << 20, 'x');
    if (mode == "long_event") event.assign(8192, 'e');
    if (mode == "empty") { text.clear(); event.clear(); }

    std::array<uint64_t, 6> message{};
    message[1] = (uintptr_t)text.data(); message[2] = text.size();
    message[4] = (uintptr_t)event.data(); message[5] = event.size();
    uint64_t wrapper = (uintptr_t)message.data();
    std::array<uint64_t, 13> step{};
    step[1] = 101;  // Step.type +0x08; status shares the upper halfword
    step[12] = (uintptr_t)&wrapper;  // Step's oneof data +0x60
    agy_block block{};
    block.book = mode == "legacy" ? bk("system_message_legacy") : bk("system_message");
    block.regs.rax = mode == "legacy" ? 1 : (uintptr_t)step.data();
    block.regs.rdi = mode == "legacy" ? (uintptr_t)step.data() : 1;
    bool expect = true;
    if (mode == "other_step") { step[1] = 1; expect = false; }
    if (mode == "bad_step") { block.regs.rax = 0x10000; expect = false; }
    if (mode == "overflow_step") { block.regs.rax = UINT64_MAX - 8; expect = false; }
    if (mode == "bad_wrapper") { step[12] = 0x10000; expect = false; }
    if (mode == "bad_message") { wrapper = 0x10000; expect = false; }
    if (mode == "bad_text") { message[1] = 0x10000; expect = false; }
    if (mode == "bad_event") { message[4] = 0x10000; expect = false; }
    if (mode == "negative_length") { message[2] = UINT64_MAX; expect = false; }
    const agy_block before = block;
    agy_on_system_message(&block);
    CHECK(!std::memcmp(&block, &before, sizeof(block)));
    CHECK(emitted == (expect ? 1 : 0));
    if (expect) {
        CHECK(kind == (mode == "legacy" ? "system_message_legacy" : "system_message"));
        CHECK(stream == (uintptr_t)step.data());
        CHECK(packet.size() >= 32 && packet.size() <= (1u << 20));
        CHECK(!std::memcmp(packet.data(), "AGSM", 4));
    }
    FILE *output = std::fopen(argv[2], "wb");
    CHECK(output);
    CHECK(std::fwrite(packet.data(), 1, packet.size(), output) == packet.size());
    CHECK(std::fclose(output) == 0);
}
