#pragma once
#include <cstdint>
namespace nll {

struct message_header {
    uint16_t magic;
    uint8_t version;
    uint8_t msg_type;
    uint32_t seq_idx;
    uint64_t send_unix_ns;
};

enum class payload {TINY = 16, SMALL = 256, MEDIUM = 1024};
} // namespace nll
