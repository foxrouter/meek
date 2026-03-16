// Copyright 2025 foxrouter
/* Simple SoapySDR readStream test: prints readStream() return values, errToStr, elapsed time */
#include <SoapySDR/Device.h>
#include <SoapySDR/Errors.h>
#include <SoapySDR/Formats.h>

#include <chrono>
#include <complex>
#include <cstdint>
#include <iostream>
#include <thread>
#include <vector>

int main(int argc, char** argv) {
  double center = 433.92e6;
  double srate = 1000000;
  double gain = 20;
  size_t block_len = 4096;
  long long timeout_us = 500000;  // 500 ms

  if (argc >= 2)
    center = std::stod(argv[1]);
  if (argc >= 3)
    srate = std::stod(argv[2]);
  if (argc >= 4)
    gain = std::stod(argv[3]);
  if (argc >= 5)
    block_len = std::stoul(argv[4]);
  if (argc >= 6)
    timeout_us = std::stoll(argv[5]);

  std::cout << "Soapy read test: center=" << center << " sps=" << srate << " gain=" << gain
            << " block_len=" << block_len << " timeout_us=" << timeout_us << std::endl;

  SoapySDRDevice* dev = SoapySDRDevice_makeStrArgs("");
  if (!dev) {
    std::cerr << "No SoapySDR device found\n";
    return 2;
  }

  SoapySDRKwargs args = {};
  SoapySDRDevice_setSampleRate(dev, SOAPY_SDR_RX, 0, srate);
  SoapySDRDevice_setFrequency(dev, SOAPY_SDR_RX, 0, center, &args);
  SoapySDRDevice_setGainMode(dev, SOAPY_SDR_RX, 0, 0);
  SoapySDRDevice_setGain(dev, SOAPY_SDR_RX, 0, gain);

  SoapySDRStream* rxStream =
      SoapySDRDevice_setupStream(dev, SOAPY_SDR_RX, SOAPY_SDR_CF32, nullptr, 0, nullptr);
  if (!rxStream) {
    std::cerr << "Failed to setup RX stream\n";
    SoapySDRDevice_unmake(dev);
    return 2;
  }
  SoapySDRDevice_activateStream(dev, rxStream, 0, 0, 0);

  std::vector<std::complex<float>> buff(block_len);
  void* buffs[1] = {buff.data()};

  for (int i = 0; i < 200; ++i) {
    auto t0 = std::chrono::steady_clock::now();
    int flags = 0;
    long long ts = 0;
    int ret = SoapySDRDevice_readStream(dev, rxStream, buffs, block_len, &flags, &ts, timeout_us);
    auto t1 = std::chrono::steady_clock::now();
    auto elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count();
    std::cout << "[" << i << "] ret=" << ret << " err=\"" << SoapySDR_errToStr(ret) << "\""
              << " elapsed_ms=" << elapsed_ms << " samples_read=" << (ret > 0 ? ret : 0)
              << " timestamp=" << ts << std::endl;
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
  }

  SoapySDRDevice_deactivateStream(dev, rxStream, 0, 0);
  SoapySDRDevice_closeStream(dev, rxStream);
  SoapySDRDevice_unmake(dev);
  return 0;
}
