import SwiftUI
import VisionKit

struct QRScannerView: View {
    let onCode: (String) -> Void
    let onCancel: () -> Void
    @State private var scannerError: String?

    var body: some View {
        ZStack(alignment: .topTrailing) {
            if DataScannerViewController.isSupported && DataScannerViewController.isAvailable {
                DataScanner(onCode: onCode) { message in
                    scannerError = message
                }
                    .ignoresSafeArea()
                VStack(spacing: 10) {
                    Image(systemName: "qrcode.viewfinder")
                        .font(.system(size: 42))
                    Text("Point the camera at the Room Agent pairing QR")
                        .font(.headline)
                        .multilineTextAlignment(.center)
                }
                .padding(18)
                .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 18))
                .padding(.horizontal, 34)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottom)
                .padding(.bottom, 60)

                if let scannerError {
                    ContentUnavailableView(
                        "Camera unavailable",
                        systemImage: "camera.fill",
                        description: Text(scannerError)
                    )
                    .padding(24)
                    .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 22))
                    .padding(24)
                }
            } else {
                ContentUnavailableView(
                    "Scanner unavailable",
                    systemImage: "qrcode",
                    description: Text("Paste the one-use pairing link on the previous screen instead.")
                )
            }

            Button(action: onCancel) {
                Image(systemName: "xmark")
                    .font(.headline)
                    .frame(width: 44, height: 44)
                    .background(.ultraThinMaterial, in: Circle())
            }
            .foregroundStyle(.primary)
            .padding(18)
            .accessibilityLabel("Close scanner")
        }
        .preferredColorScheme(.dark)
    }
}

private struct DataScanner: UIViewControllerRepresentable {
    let onCode: (String) -> Void
    let onError: (String) -> Void

    func makeCoordinator() -> Coordinator {
        Coordinator(onCode: onCode, onError: onError)
    }

    func makeUIViewController(context: Context) -> DataScannerViewController {
        let controller = DataScannerViewController(
            recognizedDataTypes: [.barcode(symbologies: [.qr])],
            qualityLevel: .balanced,
            recognizesMultipleItems: false,
            isHighFrameRateTrackingEnabled: false,
            isPinchToZoomEnabled: true,
            isGuidanceEnabled: true,
            isHighlightingEnabled: true
        )
        controller.delegate = context.coordinator
        return controller
    }

    func updateUIViewController(_ controller: DataScannerViewController, context: Context) {
        guard !controller.isScanning, !context.coordinator.failed else { return }
        do {
            try controller.startScanning()
        } catch {
            context.coordinator.fail("Room Agent could not start the camera. Close this screen and paste the pairing link instead.")
        }
    }

    static func dismantleUIViewController(
        _ controller: DataScannerViewController,
        coordinator: Coordinator
    ) {
        controller.stopScanning()
    }

    final class Coordinator: NSObject, DataScannerViewControllerDelegate {
        private let onCode: (String) -> Void
        private let onError: (String) -> Void
        private var delivered = false
        fileprivate var failed = false

        init(onCode: @escaping (String) -> Void, onError: @escaping (String) -> Void) {
            self.onCode = onCode
            self.onError = onError
        }

        fileprivate func fail(_ message: String) {
            guard !failed else { return }
            failed = true
            DispatchQueue.main.async { [onError] in onError(message) }
        }

        func dataScanner(
            _ dataScanner: DataScannerViewController,
            didAdd addedItems: [RecognizedItem],
            allItems: [RecognizedItem]
        ) {
            guard !delivered else { return }
            for item in addedItems {
                guard case .barcode(let barcode) = item,
                      let payload = barcode.payloadStringValue else { continue }
                delivered = true
                dataScanner.stopScanning()
                onCode(payload)
                return
            }
        }

        func dataScanner(
            _ dataScanner: DataScannerViewController,
            becameUnavailableWithError error: DataScannerViewController.ScanningUnavailable
        ) {
            fail("The camera scanner became unavailable. Close this screen and paste the pairing link instead.")
        }
    }
}
