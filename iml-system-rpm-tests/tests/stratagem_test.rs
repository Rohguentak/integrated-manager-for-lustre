// Copyright (c) 2020 DDN. All rights reserved.
// Use of this source code is governed by a MIT-style
// license that can be found in the LICENSE file.

use iml_system_rpm_tests::{run_fs_test, wait_for_ntp};
use iml_system_test_utils::{vagrant, SetupConfig, SetupConfigType, SystemTestError, WithSos as _};

async fn run_test(config: &vagrant::ClusterConfig) -> Result<(), SystemTestError> {
    run_fs_test(
        &config,
        &SetupConfigType::RpmSetup(SetupConfig {
            use_stratagem: true,
            branding: iml_wire_types::Branding::Whamcloud,
        }),
        vec![
            ("stratagem_server".into(), &config.mds_servers()[..]),
            ("base_monitored".into(), &config.oss_servers()[..]),
            ("stratagem_client".into(), &config.client_servers()[..]),
        ],
        vagrant::FsType::LDISKFS,
    )
    .await?;

    wait_for_ntp(&config).await?;

    Ok(())
}

#[tokio::test]
async fn test_stratagem_setup() -> Result<(), SystemTestError> {
    let config = vagrant::ClusterConfig::default();
    run_test(&config)
        .await
        .handle_test_result(
            &vec![
                &config.manager_ip()[..],
                &config.storage_server_ips()[..],
                &config.client_server_ips(),
            ]
            .concat()[..],
            "rpm_stratagem_test",
        )
        .await
}
