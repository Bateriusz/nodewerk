// Fallback uncompiled stub for shaders/compute_fixture.hlsl when the CMake DXC target
// (nw_offline_shaders) has not yet generated ${CMAKE_BINARY_DIR}/shaders/generated/compute_fixture_dxil.h.
// During a CMake build, ${CMAKE_BINARY_DIR}/shaders/generated precedes this directory on the
// include path and supplies the real DXC-compiled cs_6_0 DXIL byte array.
// If compiled without running DXC, ValidateOfflineDxilContainer rejects this stub with
// ExitCode::OfflineShaderPayloadInvalid (40) before touching D3D12.
const unsigned char kComputeFixtureDxil[] = {
    0x00, 0x00, 0x00, 0x00
};
