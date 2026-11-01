/* Exercise real response readers without starting Go or a provider. */
#include "gocall.h"
#include "symbols_gen.h"
#include "wirecap.h"
#include <array>
#include <atomic>
#include <thread>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#define CHECK(expr) do { if (!(expr)) { \
    std::fprintf(stderr, "%s:%d: %s\n", __FILE__, __LINE__, #expr); std::exit(1); \
} } while (0)

static std::vector<uint8_t> packet;
static std::string kind;
static std::array<std::atomic_bool, 2048> stack_seen{};
static std::atomic_uint stack_count{};
extern "C" void wire_emit(wire_event_t *event)
{
    CHECK(event->mode == WIRE_ASYNC);
    if (std::strcmp(event->kind, "callstack") == 0) {
        CHECK(event->len == 16 && !std::memcmp(event->data, "fixture\0", 8));
        uint64_t pc;
        std::memcpy(&pc, event->data + 8, sizeof(pc));
        CHECK(pc >= 0x1000 && pc < 0x1800);
        CHECK(!stack_seen[pc - 0x1000].exchange(true));
        stack_count.fetch_add(1);
        return;
    }
    if (std::strcmp(event->kind, "fh_update") == 0) return;
    CHECK(packet.empty());
    kind = event->kind;
    packet.assign(event->data, event->data + event->len);
}
extern "C" void wire_shutdown(void) {}

int main(int argc, char **argv)
{
    CHECK(argc == 4);
    const std::string source = argv[1], mode = argv[2];
    if (source == "stack") {
        std::array<std::thread, 8> workers;
        for (unsigned i = 0; i < workers.size(); i++) {
            workers[i] = std::thread([i] {
                for (unsigned j = 0; j < 256; j++) {
                    uint64_t frame[2] = {0, 0x1000 + i * 256 + j};
                    for (int repeat = 0; repeat < 2; repeat++)
                        agy_emit_stack("fixture", (uintptr_t)frame, 0);
                }
            });
        }
        for (auto &worker : workers) worker.join();
        CHECK(stack_count == 1024);
        FILE *output = std::fopen(argv[3], "wb");
        CHECK(output && std::fprintf(output, "%u", stack_count.load()) == 4);
        CHECK(std::fclose(output) == 0);
        return 0;
    }
    std::string text;
    for (int i = 0; i < 9000; ++i) text += "\xe4\xb8\xad\xe6\x96\x87";
    if (source == "resp_chunk") text = "{\"response\":{\"responseId\":\"fixture\",\"candidates\":[{\"content\":{\"parts\":[{\"text\":\"" + text + "\"}]},\"finishReason\":\"STOP\"}]}}";
    if (mode == "long") text.assign(2u << 20, 'x');
    agy_block block{};
    std::array<uint64_t, 3> response = {0, (uintptr_t)text.data(), text.size()};
    // Getter-verified Step.GetPlannerResponse layout; metadata is a deliberate decoy.
    std::string decoy = "tool-call-id-not-an-answer";
    std::array<uint64_t, 3> tool_call = {0, (uintptr_t)decoy.data(), decoy.size()};
    std::array<uint64_t, 10> metadata{};
    metadata[9] = (uintptr_t)tool_call.data();
    uint64_t wrapper = (uintptr_t)response.data();
    std::array<uint64_t, 13> step{};
    step[1] = 15; step[2] = (uintptr_t)metadata.data();
    step[11] = AGY_PLANNER_RESPONSE_ITAB; step[12] = (uintptr_t)&wrapper;
    if (source == "app_response") {
        block.book = bk("fh_update");
        block.regs.rdi = (uintptr_t)step.data();
        block.regs.rsi = 0xdeadbeef;  // Stale non-argument register must never supply text.
    } else {
        block.book = bk("resp_chunk");
        block.regs.rax = (uintptr_t)text.data();
        block.regs.rbx = text.size();
    }
    bool expect = true;
    if (mode == "bad_step") { block.regs.rdi = 0x10000; expect = false; }
    if (mode == "overflow_step") { block.regs.rdi = UINT64_MAX - 8; expect = false; }
    if (mode == "other_step") { step[1] = 101; expect = false; }
    if (mode == "wrong_oneof") { step[11]++; expect = false; }
    if (mode == "bad_wrapper") { step[12] = 0x10000; expect = false; }
    if (mode == "bad_response") { wrapper = 0x10000; expect = false; }
    if (mode == "bad_text") { response[1] = 0x10000; expect = false; }
    if (mode == "negative_length") { response[2] = UINT64_MAX; expect = false; }
    const agy_block before = block;
    if (source == "app_response") agy_on_fh_update(&block);
    else agy_on_resp_chunk(&block);
    CHECK(!std::memcmp(&before, &block, sizeof(block)));
    if (expect) {
        CHECK(kind == source + "_packet");
        CHECK(packet.size() == (mode == "long" ? 1u << 20 : text.size() + 16));
        CHECK(!std::memcmp(packet.data(), "AGTX", 4));
        if (mode != "long") CHECK(!std::memcmp(packet.data() + 16, text.data(), text.size()));
    } else CHECK(packet.empty());
    FILE *output = std::fopen(argv[3], "wb");
    CHECK(output);
    CHECK(std::fwrite(packet.data(), 1, packet.size(), output) == packet.size());
    CHECK(std::fclose(output) == 0);
}
