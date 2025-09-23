#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <cstring>
#include <errno.h>
#include <endian.h>
#include "common/packet.hpp"
#include "common/log.hpp"
#include <string>
using nll::log::logf;
int main() {
	int sockfd = socket(AF_INET, SOCK_DGRAM, 0);
	if (sockfd < 0) {
		logf(0, "Error creating socket in receiver.cpp.\n");
	}

	struct sockaddr_in src_addr, client_addr;
	socklen_t client_addr_len = sizeof(client_addr);
	memset(&src_addr, 0, sizeof(src_addr));
	src_addr.sin_family = AF_INET;
	src_addr.sin_port = htons(49200);
	src_addr.sin_addr.s_addr = inet_addr("192.168.1.10");
	
	int result = bind(sockfd, (const struct sockaddr *)&src_addr, sizeof(src_addr));

	if (result != 0) {
		logf(0, "Error binding socket.\n");
		exit(EXIT_FAILURE);
	}

	logf(0, "UDP server listening on port %d...\n", 49200);
	for(uint32_t i = 0; i < 10; ++i) {
		struct nll::message_header mh;
		ssize_t bytes_received = recvfrom(sockfd, (void*)&mh, sizeof(mh), 0,
						(struct sockaddr *)&client_addr, &client_addr_len);
		if (bytes_received < 0) {
			logf(0, "Recvfrom failed.\n");
			exit(EXIT_FAILURE);
		}
		std::string type;
		switch (mh.msg_type) {
			case 0: 
				type = "DATA";
			default:
				type= "OTHER";
		}
		logf(0, "[PACKET %d]: %s v%d. type=%s, time=%d", be64toh(mh.seq_idx), std::to_string(ntohs(mh.magic)), mh.version, type, mh.send_unix_ns);
	}
	close(sockfd);
	return 0;
}
