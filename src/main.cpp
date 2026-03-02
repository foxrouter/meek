// Modify the default block length and read timeout configuration

int main() {
    // Read block length from environment variables. Default to 8192.
    size_t block_len = env_to_sz("RF_BLOCK_LEN", env_to_sz("BLOCK_LEN", 8192));

    // Read read timeout from environment variables. Default to 200000.
    int64_t read_timeout_us = env_to_ll("RF_READ_TIMEOUT_US", env_to_ll("READ_TIMEOUT_US", 200000));

    // Rest of the code...
