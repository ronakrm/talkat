# CPU Optimization Guide for Talkat

This guide explains how Talkat is optimized for CPU-only operation and provides performance recommendations.

## Why CPU-Only?

Talkat defaults to CPU-only operation for several reasons:

1. **Broader Compatibility**: Works on any machine without GPU requirements
2. **Sufficient Performance**: Real-time audio transcription doesn't require GPU for single-stream processing
3. **Reduced Dependencies**: CPU-only PyTorch is ~500MB vs ~2GB for CUDA version
4. **Lower Power Consumption**: Better for laptops and continuous operation
5. **Cost Effective**: No need for expensive GPU hardware

## Performance on CPU

### Expected Performance by Model

| Model | CPU Performance | Accuracy | RAM Usage | Recommended For |
|-------|----------------|----------|-----------|-----------------|
| **Faster-Whisper base.en** | 4-8x real-time | Good | ~1GB | General use, default |
| **Distil-Whisper medium.en** | 3-6x real-time | Better | ~1.5GB | Better accuracy |
| **Distil-Whisper small.en** | 6-10x real-time | Good | ~900MB | Faster response |
| **Vosk small** | 10-20x real-time | Moderate | ~500MB | Low resource |

*Real-time = 1 second of audio processed in 1 second*

### CPU Requirements

**Minimum Requirements**:
- 2 CPU cores
- 4GB RAM
- x86_64 architecture
- AVX instructions support (most CPUs after 2011)

**Recommended Requirements**:
- 4+ CPU cores
- 8GB RAM
- Intel i5/AMD Ryzen 5 or better
- AVX2 support for better performance

## Installation for CPU-Only

The default installation now uses CPU-only PyTorch:

```bash
# Standard installation - CPU only
uv sync

# This installs torch from PyTorch CPU index
# Size: ~500MB instead of ~2GB for CUDA version
```

## Configuration for Best CPU Performance

### 1. Use Appropriate Model Size

For CPU, smaller models often provide better user experience:

```json
{
  "model_type": "faster-whisper",
  "model_name": "base.en",
  "fw_compute_type": "int8"
}
```

Or for Distil-Whisper:

```json
{
  "model_type": "distil-whisper",
  "distil_model_name": "distil-whisper/distil-small.en",
  "device": "cpu"
}
```

### 2. Optimize Faster-Whisper for CPU

Faster-Whisper has specific CPU optimizations:

```json
{
  "model_type": "faster-whisper",
  "model_name": "base.en",
  "fw_device": "cpu",
  "fw_compute_type": "int8",  // int8 quantization for speed
  "fw_device_index": 0
}
```

**Compute Type Options**:
- `int8`: Fastest, slight accuracy loss (recommended for CPU)
- `int16`: Good balance
- `float32`: Best accuracy, slowest

### 3. VAD Settings for CPU

Adjust Voice Activity Detection for CPU efficiency:

```json
{
  "silence_threshold": 300.0,  // Higher threshold = less processing
  "silence_duration": 1.0       // Shorter = more responsive
}
```

## Model Recommendations by Use Case

### General Dictation (Recommended)
```json
{
  "model_type": "faster-whisper",
  "model_name": "base.en",
  "fw_compute_type": "int8"
}
```
- Fast response time
- Good accuracy
- Low resource usage

### High Accuracy Needs
```json
{
  "model_type": "distil-whisper",
  "distil_model_name": "distil-whisper/distil-medium.en",
  "device": "cpu"
}
```
- Better accuracy
- Slower but still real-time
- Higher memory usage

### Low Resource Systems
```json
{
  "model_type": "vosk",
  "model_name": "vosk-model-small-en-us-0.15"
}
```
- Minimal resource usage
- Very fast
- Moderate accuracy

### Long Dictation Sessions
```json
{
  "model_type": "faster-whisper",
  "model_name": "tiny.en",
  "fw_compute_type": "int8"
}
```
- Extremely fast
- Low memory footprint
- Good for extended use

## Performance Tuning Tips

### 1. CPU Governor Settings (Linux)

Set CPU to performance mode for better response:

```bash
# Check current governor
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor

# Set to performance (requires root)
sudo cpupower frequency-set -g performance

# Or for all CPUs
for cpu in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
  echo performance | sudo tee $cpu
done
```

### 2. Process Priority

Run talkat with higher priority:

```bash
# Start server with nice priority
nice -n -10 uv run talkat server

# Or use renice for running process
sudo renice -n -10 -p $(pgrep -f "talkat server")
```

### 3. Disable CPU Throttling

For laptops, ensure maximum performance:

```bash
# Intel CPUs - disable throttling
echo 1 | sudo tee /sys/module/processor/parameters/ignore_ppc

# Check thermal throttling
cat /sys/class/thermal/thermal_zone*/temp
```

### 4. Memory Optimization

Pre-allocate memory and reduce swapping:

```bash
# Reduce swappiness
echo 10 | sudo tee /proc/sys/vm/swappiness

# Clear cache before starting
sync && echo 3 | sudo tee /proc/sys/vm/drop_caches
```

## Monitoring Performance

### CPU Usage
```bash
# Monitor during transcription
htop
# or
top -p $(pgrep -f "talkat server")
```

### Response Time Testing
```bash
# Time a transcription
time uv run talkat file test.wav

# Monitor with built-in stats
uv run talkat long  # Shows WPM and stats
```

### Temperature Monitoring
```bash
# Watch CPU temperature
watch -n 1 sensors

# Or
cat /sys/class/thermal/thermal_zone*/temp
```

## Troubleshooting CPU Performance

### Issue: Slow Transcription
**Solutions**:
1. Switch to smaller model (tiny.en or base.en)
2. Use int8 quantization
3. Check CPU throttling
4. Close other applications

### Issue: High CPU Usage
**Solutions**:
1. Use Vosk for lower CPU usage
2. Increase silence_threshold
3. Use smaller model
4. Check for background processes

### Issue: Delayed Response
**Solutions**:
1. Reduce model size
2. Adjust VAD settings
3. Set CPU governor to performance
4. Increase process priority

## Benchmarks on Common CPUs

| CPU | Model | Speed | Notes |
|-----|-------|-------|-------|
| Intel i5-8250U (Laptop) | base.en | 4x real-time | Good for general use |
| Intel i7-10700K (Desktop) | base.en | 8x real-time | Excellent performance |
| AMD Ryzen 5 5600X | base.en | 10x real-time | Very responsive |
| Apple M1 | base.en | 12x real-time | Exceptional efficiency |
| Intel i3-7100U (Old Laptop) | tiny.en | 3x real-time | Usable with small model |

## GPU Users (Optional)

If you have a GPU and want to use it:

```bash
# Install CUDA version of PyTorch
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118

# Update config
echo '{"device": "cuda"}' >> ~/.config/talkat/config.json
```

However, for voice dictation, GPU provides minimal benefit unless:
- Processing multiple streams simultaneously
- Using very large models (large-v3)
- Batch processing many files

## Summary

Talkat is optimized for CPU-only operation, providing:
- Real-time transcription on modern CPUs
- Reduced installation size (~500MB saved)
- Broader compatibility
- Lower power consumption
- No GPU dependencies

For most users, CPU performance is more than sufficient for real-time voice dictation. The default settings are optimized for the best balance of speed and accuracy on CPU.