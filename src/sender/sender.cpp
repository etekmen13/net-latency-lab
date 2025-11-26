#include <arpa/inet.h>
#include <cstring>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include "common/log.hpp"
#include "common/packet.hpp"
#include "common/time.hpp"

struct ScopedSocket {
  int fd;
  ScopedSocket() : fd(socket(AF_INET, SOCK_DGRAM, 0)) {}
  ~ScopedSocket() {
    if (fd >= 0)
      close(fd);
  }
  operator int() const { return fd; }
};

int main(int argc, char **argv) {
  std::string dest_ip = "127.0.0.1";
  if (argc > 1)
    dest_ip = argv[1];

  uint16_t port = 49200;
  ScopedSocket sock;

  sockaddr_in dest_addr{};
  dest_addr.sin_family = AF_INET;
  dest_addr.sin_port = htons(port);
  dest_addr.sin_addr.s_addr = inet_addr(dest_ip.c_str());

  NLL_INFO("Sending to %s:%d at steady rate (1ms interval)...\n",
           dest_ip.c_str(), port);

  uint32_t seq = 0;
  const uint64_t interval_ns = 1'000'000;

  while (true) {
    uint64_t start_loop = nll::mono_ns();

    nll::message_header mh;
    mh.magic = 0x6584;
    mh.version = 1;
    mh.msg_type = 0;
    mh.seq_idx = seq++;
    mh.send_unix_ns = nll::real_ns();

    mh.to_network();

    ssize_t sent = sendto(sock, &mh, sizeof(mh), 0,
                          (struct sockaddr *)&dest_addr, sizeof(dest_addr));

    if (sent < 0)
      NLL_ERROR("Send failed");

    uint64_t end_loop = nll::mono_ns();
    uint64_t elapsed = end_loop - start_loop;

    if (elapsed < interval_ns) {
      nll::sleep_ns(interval_ns - elapsed);
    } else {
      NLL_WARN("Can't keep up! Loop took %llu ns\n", elapsed);
    }
  }

  return 0;
}
