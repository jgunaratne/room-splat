// Variance-of-Laplacian blur metric fed to KeyframeSelector (SPEC.md M1).
//
// Computed on the luma (Y) plane of the ARKit capturedImage, subsampled so it costs
// a fraction of a millisecond per frame rather than processing the full image.

import CoreVideo

enum BlurScore {
    /// Higher is sharper. Returns the variance of a discrete Laplacian over the
    /// subsampled luma plane. Assumes a 420 bi-planar YCbCr buffer (plane 0 = luma).
    static func laplacianVariance(_ pixelBuffer: CVPixelBuffer) -> Float {
        CVPixelBufferLockBaseAddress(pixelBuffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pixelBuffer, .readOnly) }

        guard let base = CVPixelBufferGetBaseAddressOfPlane(pixelBuffer, 0) else { return 0 }
        let width = CVPixelBufferGetWidthOfPlane(pixelBuffer, 0)
        let height = CVPixelBufferGetHeightOfPlane(pixelBuffer, 0)
        let rowBytes = CVPixelBufferGetBytesPerRowOfPlane(pixelBuffer, 0)
        let ptr = base.assumingMemoryBound(to: UInt8.self)

        let step = 4
        @inline(__always) func luma(_ x: Int, _ y: Int) -> Float { Float(ptr[y * rowBytes + x]) }

        var sum: Float = 0, sumSq: Float = 0, n: Float = 0
        var y = step
        while y < height - step {
            var x = step
            while x < width - step {
                let lap = 4 * luma(x, y)
                    - luma(x - step, y) - luma(x + step, y)
                    - luma(x, y - step) - luma(x, y + step)
                sum += lap
                sumSq += lap * lap
                n += 1
                x += step
            }
            y += step
        }
        guard n > 0 else { return 0 }
        let mean = sum / n
        return sumSq / n - mean * mean
    }
}
