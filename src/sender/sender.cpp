#include <arpa/inet.h>
#include <cstring>
#include <getopt.h>
#include <iostream>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include "common/log.hpp"
#include "common/packet.hpp"
#include "common/thread_utils.hpp"
#include "common/time.hpp"

struct SenderConfig {
  std::string dest_ip = "127.0.0.1";
  uint16_t port = 49200;
  uint32_t rate_pps = 1000;
  std::string mode = "steady"; // steady | burst
  uint32_t burst_size = 1;
  uint32_t duration_sec = 10;
};

void print_usage() {
  printf("Usage: sender --ip <IP> --rate <PPS> --mode <steady|burst> "
         "--duration <SEC> [--burst <N>]\n");
}

int main(int argc, char **argv) {
  SenderConfig config;

  struct option long_options[] = {{"ip", required_argument, 0, 'i'},
                                  {"port", required_argument, 0, 'p'},
                                  {"rate", required_argument, 0, 'r'},
                                  {"mode", required_argument, 0, 'm'},
                                  {"burst", required_argument, 0, 'b'},
                                  {"duration", required_argument, 0, 'd'},
                                  {0, 0, 0, 0}};

  int opt;
  while ((opt = getopt_long(argc, argv, "i:p:r:m:b:d:", long_options,
                            nullptr)) != -1) {
    switch (opt) {
    case 'i':
      config.dest_ip = optarg;
      break;
    case 'p':
      config.port = std::stoi(optarg);
      break;
    case 'r':
      config.rate_pps = std::stoi(optarg);
      break;
    case 'm':
      config.mode = optarg;
      break;
    case 'b':
      config.burst_size = std::stoi(optarg);
      break;
    case 'd':
      config.duration_sec = std::stoi(optarg);
      break;
    default:
      print_usage();
      return 1;
    }
  }

  int sock = socket(AF_INET, SOCK_DGRAM, 0);
  sockaddr_in dest_addr{};
  dest_addr.sin_family = AF_INET;
  dest_addr.sin_port = htons(config.port);
  inet_pton(AF_INET, config.dest_ip.c_str(), &dest_addr.sin_addr);

  NLL_INFO("Configuration:\n");
  NLL_INFO("  Dest: %s:%d\n", config.dest_ip.c_str(), config.port);
  NLL_INFO("  Rate: %u pps\n", config.rate_pps);
  NLL_INFO("  Mode: %s (Burst: %u)\n", config.mode.c_str(), config.burst_size);
  NLL_INFO("  Dur : %u sec\n", config.duration_sec);

  uint64_t interval_ns = 1'000'000'000ULL / config.rate_pps;
  uint64_t total_ns = config.duration_sec * 1'000'000'000ULL;

  uint32_t seq = 0;
  uint64_t start_time = nll::mono_ns();
  uint64_t end_time = start_time + total_ns;
  uint64_t next_tx_time = start_time;

  nll::thread::pin_to_core(1);

  nll::message_header mh;
  mh.magic = 0x6584;
  mh.version = 1;
  mh.msg_type = 0;

  while (nll::mono_ns() < end_time) {
    uint64_t now = nll::mono_ns();

    if (now >= next_tx_time) {

      uint32_t packets_to_send =
          (config.mode == "burst") ? config.burst_size : 1;

      for (uint32_t i = 0; i < packets_to_send; ++i) {
        mh.seq_idx = seq++;
        mh.send_unix_ns = nll::real_ns();
        mh.to_network();

        sendto(sock, &mh, sizeof(mh), 0, (struct sockaddr *)&dest_addr,
               sizeof(dest_addr));

        mh.to_host();
      }

      next_tx_time += (interval_ns * packets_to_send);

    } else {
      uint64_t remaining = next_tx_time - now;
      if (remaining > 1'000'000) {
        nll::sleep_ns(remaining - 200'000);
      } else {
        nll::thread::cpu_relax();
      }
    }
  }

  NLL_INFO("Finished. Sent %u packets.\n", seq);
  close(sock);
  return 0;
}
