/* Simple SoapySDR readStream test: prints readStream() return values, errToStr, elapsed time */
#include <SoapySDR/Device.hpp>
#include <SoapySDR/Formats.hpp>
#include <SoapySDR/Errors.hpp>
#include <chrono>
#include <complex>
#include <iostream>
#include <thread>
#include <vector>

int main(int argc, char** argv){
    double center = 433.92e6;
    double srate = 1000000;
    double gain = 20;
    size_t block_len = 4096;
    long long timeout_us = 500000; // 500 ms

    if (argc >= 2) center = std::stod(argv[1]);
    if (argc >= 3) srate = std::stod(argv[2]);
    if (argc >= 4) gain = std::stod(argv[3]);
    if (argc >= 5) block_len = std::stoul(argv[4]);
    if (argc >= 6) timeout_us = std::stoll(argv[5]);

    std::cout << "Soapy read test: center=" << center << " sps=" << srate
              << " gain=" << gain << " block_len=" << block_len
              << " timeout_us=" << timeout_us << std::endl;

    SoapySDR::Kwargs kw;
    SoapySDR::Device *dev = nullptr;
    try {
        dev = SoapySDR::Device::make(kw);
        if (!dev) {
            std::cerr << "No SoapySDR device found\n";
            return 2;
        }
    } catch (const std::exception &ex){
        std::cerr << "SoapySDR::Device::make() threw: " << ex.what() << std::endl;
        return 2;
    }

    dev->setSampleRate(SOAPY_SDR_RX, 0, srate);
    dev->setFrequency(SOAPY_SDR_RX, 0, center);
    dev->setGain(SOAPY_SDR_RX, 0, gain);

    SoapySDR::Stream *rxStream = dev->setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32);
    if (!rxStream) {
        std::cerr << "Failed to setup RX stream\n";
        SoapySDR::Device::unmake(dev);
        return 2;
    }
    dev->activateStream(rxStream, 0, 0, 0);

    std::vector<std::complex<float>> buff(block_len);
    void* buffs[1] = { buff.data() };

    for (int i=0; i<200; ++i) {
        auto t0 = std::chrono::steady_clock::now();
        int flags = 0;
        long long ts = 0;
        int ret = dev->readStream(rxStream, (void**)&buffs[0], (int)block_len, flags, ts, timeout_us);
        auto t1 = std::chrono::steady_clock::now();
        auto elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count();
        std::cout << "[" << i << "] ret=" << ret
                  << " err=\"" << SoapySDR::errToStr(ret) << "\""
                  << " elapsed_ms=" << elapsed_ms
                  << " samples_read=" << (ret>0? ret:0)
                  << " timestamp=" << ts
                  << std::endl;
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }

    dev->deactivateStream(rxStream, 0, 0);
    dev->closeStream(rxStream);
    SoapySDR::Device::unmake(dev);
    return 0;
}
