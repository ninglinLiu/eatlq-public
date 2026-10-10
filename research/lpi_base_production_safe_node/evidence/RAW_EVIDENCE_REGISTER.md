# Raw Evidence Register

generated_at_utc = 2026-07-17T14:43:53Z
entries = 89
inventory = evidence/raw_evidence_inventory.tsv
generator = scripts/rebuild_evidence_register.py

| relative_path | size_bytes | sha256 | classification | retention_policy | actual_source_round |
|---|---:|---|---|---|---|
| `reports/raw/bootstrap_health_post_20260717T1315Z/snapshot.txt` | 1396 | `0944d490a012b486eb77de9704a704f65833995309ec3153e04ce076e4f4870f` | REFERENCED_BOOTSTRAP_HEALTH_SNAPSHOT | seal_in_git_permanent | bootstrap_health_probe |
| `reports/raw/bootstrap_health_pre_20260717/capacity_samples.tsv` | 653 | `3adb9cd21ab90615a366d3e0f767d2b4860dac8fbe98ccc14607c373175b27bd` | PRIOR_BOOTSTRAP_HEALTH | seal_in_git_archive | bootstrap_health_probe |
| `reports/raw/bootstrap_health_pre_20260717/executor_consumer_samples.txt` | 105 | `c63c430c09fd0437448b53fa2a418211a58f4d824b8c06e45f51fdd0fdb1e2dc` | PRIOR_BOOTSTRAP_HEALTH | seal_in_git_archive | bootstrap_health_probe |
| `reports/raw/bootstrap_health_pre_20260717/guard_events.txt` | 41 | `75f0066735b527993e15404c7f765cc95807b6decd64081699fcc5fa0b2cac03` | PRIOR_BOOTSTRAP_HEALTH | seal_in_git_archive | bootstrap_health_probe |
| `reports/raw/bootstrap_health_pre_20260717/pidstat_samples.txt` | 124225 | `ba022a11e6bfffeee157163284b2c938d5c5b85bc4d5ecebd3fbc0fd862c9efa` | PRIOR_BOOTSTRAP_HEALTH | seal_in_git_archive | bootstrap_health_probe |
| `reports/raw/bootstrap_health_pre_20260717T1311Z/snapshot.txt` | 2083 | `fdddfdb7b5e225e3bfab39525baedc72ec730934a611032c665a46d86cc12992` | REFERENCED_BOOTSTRAP_HEALTH_SNAPSHOT | seal_in_git_permanent | bootstrap_health_probe |
| `reports/raw/bootstrap_health_summary.tsv` | 753 | `455a8742fc60f6302b8da853fcf5fae010cd3b9b8fe3c91c3b90d3e040dcf696` | PRIOR_BOOTSTRAP_HEALTH | seal_in_git_archive | bootstrap_health_probe |
| `reports/raw/clanker_1000_block_probe_preflight.json` | 1795 | `e08e4960d1bffc5019f8cdfd5c076bce989543e85298efa05d39b36567510976` | CLANKER_OFFLINE_PREFLIGHT | seal_in_git_permanent | clanker_offline_preflight |
| `reports/raw/final_secret_scan.txt` | 163 | `1f7aa09273247e21060a815614b996d635fd766707d83871d017959327d035eb` | SECRET_SCAN_ARTIFACT | seal_in_git_archive | correction_round_offline_test |
| `reports/raw/live_capacity_20260717T1055Z/capacity_samples.tsv` | 7008 | `9e919d5bae1da27257daaaf98114b88e52ec780cce2f5f54158a6329fcf0b81b` | PRIOR_LIVE_CAPACITY_OBSERVE | seal_in_git_archive_do_not_re_run_script | prior_live_capacity_observe |
| `reports/raw/live_capacity_20260717T1055Z/executor_consumer_samples.txt` | 1567 | `b8ee5bdeb3607089a0645d647d4e363e5ed373007e78367a66dc16f0a347ff8b` | PRIOR_LIVE_CAPACITY_OBSERVE | seal_in_git_archive_do_not_re_run_script | prior_live_capacity_observe |
| `reports/raw/live_capacity_20260717T1055Z/guard_events.txt` | 83 | `e739833768633f4fe33e14af46cedda6ea0778264838fb7f8a8b2983b422ca2d` | PRIOR_LIVE_CAPACITY_OBSERVE | seal_in_git_archive_do_not_re_run_script | prior_live_capacity_observe |
| `reports/raw/live_capacity_20260717T1055Z/pidstat_samples.txt` | 1822565 | `88719fab2bc1d9bfc0fecf0612ab7d971ec82bd22f6343817bea4ab3fa3189df` | PRIOR_LIVE_CAPACITY_OBSERVE | seal_in_git_archive_do_not_re_run_script | prior_live_capacity_observe |
| `reports/raw/live_capacity_20260717T1146Z/capacity_samples.tsv` | 8830 | `424b69292acb604c057c93a08a566ccb149cf1e6ed5bc375044e8348a4a3cff8` | PRIOR_LIVE_CAPACITY_OBSERVE | seal_in_git_archive_do_not_re_run_script | prior_live_capacity_observe |
| `reports/raw/live_capacity_20260717T1146Z/executor_consumer_samples.txt` | 1984 | `7d080e76754a00cb82b46d24e35e13cabb5e952981adf58c2f934f39c9423c44` | PRIOR_LIVE_CAPACITY_OBSERVE | seal_in_git_archive_do_not_re_run_script | prior_live_capacity_observe |
| `reports/raw/live_capacity_20260717T1146Z/guard_events.txt` | 41 | `1ddf83150a631c120345aaff53e6f0e6e7da2bd341c6c6bb7b8169763f343235` | PRIOR_LIVE_CAPACITY_OBSERVE | seal_in_git_archive_do_not_re_run_script | prior_live_capacity_observe |
| `reports/raw/live_capacity_20260717T1146Z/pidstat_samples.txt` | 2333993 | `17176177a57b623ad1cdcd67041348b68054412ddbeb246aed7222ba6f42e25c` | PRIOR_LIVE_CAPACITY_OBSERVE | seal_in_git_archive_do_not_re_run_script | prior_live_capacity_observe |
| `reports/raw/live_capacity_20260717T1146Z/process_aggregates.txt` | 42333 | `6bd07c68245775d0271ab71ce39af52f5d3f4a0b12d8dfbc2bb6a8f465ca134d` | PRIOR_LIVE_CAPACITY_OBSERVE | seal_in_git_archive_do_not_re_run_script | prior_live_capacity_observe |
| `reports/raw/offline_secret_scan.txt` | 0 | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` | PRIOR_OFFLINE_TEST | seal_in_git_archive | prior_offline_test |
| `reports/raw/offline_test_combined.txt` | 7726 | `544106103e2806aabe309c242631eb674891057fc1282700c3128d8745f1287a` | PRIOR_OFFLINE_TEST | seal_in_git_archive | prior_offline_test |
| `reports/raw/offline_test_health_post_20260717/capacity_samples.tsv` | 661 | `8b63adf4540ba2f1d57ac617885bb06d97200323f942b6b7e574e04cb5dd559b` | PRIOR_OFFLINE_TEST | seal_in_git_archive | prior_offline_test |
| `reports/raw/offline_test_health_post_20260717/executor_consumer_samples.txt` | 105 | `94f01987b9296221bbae627dbe9ff5886152be2facfe430f1d74226740c7230e` | PRIOR_OFFLINE_TEST | seal_in_git_archive | prior_offline_test |
| `reports/raw/offline_test_health_post_20260717/guard_events.txt` | 41 | `f891434f7d792de46113c9658fa0f7f3ed681b9358dbba95af0d91ca70c49dd2` | PRIOR_OFFLINE_TEST | seal_in_git_archive | prior_offline_test |
| `reports/raw/offline_test_health_post_20260717/pidstat_samples.txt` | 119085 | `7af808cbeedbfe9272b1666d6863360a98b248377bd6c02b5de7e8ec39e3339e` | PRIOR_OFFLINE_TEST | seal_in_git_archive | prior_offline_test |
| `reports/raw/offline_test_health_pre_20260717/capacity_samples.tsv` | 651 | `56e153cbb5b977ddd61c42d14e322c6611fa621466be7d7385011a7695dd8fcb` | PRIOR_OFFLINE_TEST | seal_in_git_archive | prior_offline_test |
| `reports/raw/offline_test_health_pre_20260717/executor_consumer_samples.txt` | 104 | `34a6ed07ebe04d109ccd19055c6a7ce1c1f21eff74bb5b38c5d3d590861dd103` | PRIOR_OFFLINE_TEST | seal_in_git_archive | prior_offline_test |
| `reports/raw/offline_test_health_pre_20260717/guard_events.txt` | 41 | `0478ddfc6ce1524cc91479372f4d4473ae101e2e0fb832ebc7a97e663dc10831` | PRIOR_OFFLINE_TEST | seal_in_git_archive | prior_offline_test |
| `reports/raw/offline_test_health_pre_20260717/pidstat_samples.txt` | 124043 | `822dacb3713319d84a690c629426462a322fdcb085932ff4eced18c17d39926e` | PRIOR_OFFLINE_TEST | seal_in_git_archive | prior_offline_test |
| `reports/raw/offline_test_output.txt` | 2047 | `6ea7ce49aae36ad9527223b8d892dd9ad73326b8636c8fc8928a752a6690e9be` | PRIOR_OFFLINE_TEST | seal_in_git_archive | prior_offline_test |
| `reports/raw/offline_test_resource.txt` | 821 | `e7098c5a1866fd927947e6db8a28da0b30d4e8e31e5787ab7234b582c71860fc` | PRIOR_OFFLINE_TEST | seal_in_git_archive | prior_offline_test |
| `reports/raw/offline_tests_20260717T132747Z/combined.txt` | 11606 | `e5b8423e7e9600ef3e04a574350c04dc74ba66fffb3395a98a78ae73c885ae3e` | CORRECTION_ROUND_OFFLINE_TEST | seal_in_git_permanent | correction_round_offline_test |
| `reports/raw/offline_tests_20260717T132747Z/summary.txt` | 180 | `ab099bddd03421b53e2de17c84547d5c598b1b60c3aa930c30e6edf7f4c5d14a` | CORRECTION_ROUND_OFFLINE_TEST | seal_in_git_permanent | correction_round_offline_test |
| `reports/raw/offline_tests_20260717T132747Z/unittest_stderr_time.txt` | 7330 | `32f6410f6664115b7b103e2e3cbea043c8eee02a09b999d1aadd8955ae0aca6e` | CORRECTION_ROUND_OFFLINE_TEST | seal_in_git_permanent | correction_round_offline_test |
| `reports/raw/offline_tests_20260717T132747Z/unittest_stdout.txt` | 4276 | `c01cef212b9d7df6704cb4dc1b457ca15b55bbf53e8bfec36476c76ae86e2944` | CORRECTION_ROUND_OFFLINE_TEST | seal_in_git_permanent | correction_round_offline_test |
| `reports/raw/offline_tests_20260717T132840Z/combined.txt` | 11606 | `0fe7a2310befc755225e931a98e4986f6dae7ee46d06969fb54b8917acda45f9` | CORRECTION_ROUND_OFFLINE_TEST | seal_in_git_permanent | correction_round_offline_test |
| `reports/raw/offline_tests_20260717T132840Z/summary.txt` | 180 | `7063c28d3f671684b92d55e70dd0517884bb480922fac5ab6f1331148d9239e8` | CORRECTION_ROUND_OFFLINE_TEST | seal_in_git_permanent | correction_round_offline_test |
| `reports/raw/offline_tests_20260717T132840Z/unittest_stderr_time.txt` | 7330 | `9bf1d7fd57cc4d6f664164067e2509bb73824b113b842ae2c2842e14baff6594` | CORRECTION_ROUND_OFFLINE_TEST | seal_in_git_permanent | correction_round_offline_test |
| `reports/raw/offline_tests_20260717T132840Z/unittest_stdout.txt` | 4276 | `c01cef212b9d7df6704cb4dc1b457ca15b55bbf53e8bfec36476c76ae86e2944` | CORRECTION_ROUND_OFFLINE_TEST | seal_in_git_permanent | correction_round_offline_test |
| `reports/raw/offline_tests_20260717T134024Z/combined.txt` | 13228 | `bd7ef8fcd17853d9dd1f1ab75627bd0de26ce1cc0e6243de4361d021d5f2e891` | CORRECTION_ROUND_OFFLINE_TEST | seal_in_git_permanent | correction_round_offline_test |
| `reports/raw/offline_tests_20260717T134024Z/summary.txt` | 180 | `5f9cda32cc759a830f46d9f2ca867c4774cf50fa4aecacc427514bd938743e7a` | CORRECTION_ROUND_OFFLINE_TEST | seal_in_git_permanent | correction_round_offline_test |
| `reports/raw/offline_tests_20260717T134024Z/unittest_stderr_time.txt` | 8952 | `6b010d1e63a25b573eabe6eeff55ecf550ab632a1dd4c1480a7585d489daaef1` | CORRECTION_ROUND_OFFLINE_TEST | seal_in_git_permanent | correction_round_offline_test |
| `reports/raw/offline_tests_20260717T134024Z/unittest_stdout.txt` | 4276 | `c01cef212b9d7df6704cb4dc1b457ca15b55bbf53e8bfec36476c76ae86e2944` | CORRECTION_ROUND_OFFLINE_TEST | seal_in_git_permanent | correction_round_offline_test |
| `reports/raw/offline_tests_20260717T135359Z/combined.txt` | 14368 | `6173857908149ef528080772980b7668b77cc1c24da4b7f6db5cb30c63abab78` | FINAL_OFFLINE_CLOSEOUT_TEST | seal_in_git_permanent | final_offline_closeout_test |
| `reports/raw/offline_tests_20260717T135359Z/summary.txt` | 180 | `13475baea80b3ecd1d8252698fea8d5b29073ce99f472e0760a044757b4446c5` | FINAL_OFFLINE_CLOSEOUT_TEST | seal_in_git_permanent | final_offline_closeout_test |
| `reports/raw/offline_tests_20260717T135359Z/unittest_stderr_time.txt` | 10092 | `1372a2ea01ec3545c1a4a07bb21f106ed169cc1812c9733ab80996af8023f7cb` | FINAL_OFFLINE_CLOSEOUT_TEST | seal_in_git_permanent | final_offline_closeout_test |
| `reports/raw/offline_tests_20260717T135359Z/unittest_stdout.txt` | 4276 | `c01cef212b9d7df6704cb4dc1b457ca15b55bbf53e8bfec36476c76ae86e2944` | FINAL_OFFLINE_CLOSEOUT_TEST | seal_in_git_permanent | final_offline_closeout_test |
| `reports/raw/offline_tests_20260717T141001Z/combined.txt` | 19958 | `eebd0f03aa74d064b545692aa5c9eaf0c007b640a5d14ae8ccb50076c3630c10` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T141001Z/run_metadata.json` | 188 | `b235b60635e797d0d1f022bf975dc0714123d08ba94037f2940534472fe9c691` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T141001Z/summary.txt` | 180 | `d1eff44bbe7571390ee4cfc8d8056afc04948e726b2c1631c0c6f266d524c6cc` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T141001Z/unittest_stderr_time.txt` | 15682 | `072e621cbf40d7372aa06b876081bb5f7038349d2319b3e0f76954ba985549cc` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T141001Z/unittest_stdout.txt` | 4276 | `c01cef212b9d7df6704cb4dc1b457ca15b55bbf53e8bfec36476c76ae86e2944` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T141051Z/combined.txt` | 17708 | `491606e6576ec4ab5de4b2838cebcdd9c50f0b7651e5b69bda7a1cfba0d59b33` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T141051Z/run_metadata.json` | 188 | `addb78595bcbffce3c1071cf72798c5deb49ad2e149438ebfad4ddd0039c3adc` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T141051Z/summary.txt` | 180 | `e814f2075be4e0496a391b5f6126e3c378f39f989a2947d1342c4e729a46108d` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T141051Z/unittest_stderr_time.txt` | 13432 | `fde8d8d6c9f6dee2e6755bdb5a782b69cb1f520806b378929b763a96a8c73bc2` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T141051Z/unittest_stdout.txt` | 4276 | `c01cef212b9d7df6704cb4dc1b457ca15b55bbf53e8bfec36476c76ae86e2944` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T141434Z/combined.txt` | 18419 | `bed82c285e13ab0619deb37c1cc68e9f16d10a1a27cd11a358ba9319d4b5361d` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T141434Z/run_metadata.json` | 188 | `a94495447efd3a6398476e0baaf092f5ae9fb21bb67b8ff27c54d405280e8536` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T141434Z/summary.txt` | 180 | `4964ea4c1c1332201d265be128b80b3da3eced1a37d9284ed27fd4e241407e2a` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T141434Z/unittest_stderr_time.txt` | 14143 | `112e0c79159fbf91762e055ed81d9601d726a60a77586e1e68170356bf9e27cc` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T141434Z/unittest_stdout.txt` | 4276 | `c01cef212b9d7df6704cb4dc1b457ca15b55bbf53e8bfec36476c76ae86e2944` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T141452Z/combined.txt` | 17655 | `7e187913649b1dba39bc6eb8267d556ae77adfed1552283724e2fa4db5264580` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T141452Z/run_metadata.json` | 188 | `4c534e3a0f2750227897c637ec999ac1af89eed26ab065ddf087495d19498e05` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T141452Z/summary.txt` | 180 | `08e7f746f5905417f4cc6e3d26a6a2ea503dbe823831a88ace71a31de4442e6e` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T141452Z/unittest_stderr_time.txt` | 13379 | `e6592f6c4e726f69713a61db4de442cd9318dc1a7eb25328e8da358e402c4c83` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T141452Z/unittest_stdout.txt` | 4276 | `c01cef212b9d7df6704cb4dc1b457ca15b55bbf53e8bfec36476c76ae86e2944` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T142915Z/combined.txt` | 20949 | `c2d6edefb40d6996bf1989e91a8b219acef7d1cac3ac47120389f28f581ea236` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T142915Z/run_metadata.json` | 188 | `72602339818210d683f7f6b67fae8904fad70f6cee409cd8f64b568f5b749ea8` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T142915Z/summary.txt` | 180 | `3f3a38a693cf81065c523db4f1b0094b4edb3a48c131790d47562b181d92d161` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T142915Z/unittest_stderr_time.txt` | 16673 | `c36808e627d7ce994e5e9bfaeca9d0721bbf08318d02b093375594a8ec24f25b` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T142915Z/unittest_stdout.txt` | 4276 | `c01cef212b9d7df6704cb4dc1b457ca15b55bbf53e8bfec36476c76ae86e2944` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T144259Z/combined.txt` | 21080 | `9da98facbac596dcc3f9cfb07594335a8c4c3af4032172624d7e84e97c6d5deb` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T144259Z/run_metadata.json` | 188 | `b4d264d062c2f23cf3724dc95c28bf8a27ad5b74c383464ffc30647f49f08e52` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T144259Z/summary.txt` | 180 | `35ead2616de5912c455ac7528310cd494e1f3840eff2bc9ed91f9b07e40bcb0a` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T144259Z/unittest_stderr_time.txt` | 16804 | `4b876b68d0c90469fb0af9ad5280d91af00c2c8772b82541dc6cf5275b5e3f7f` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T144259Z/unittest_stdout.txt` | 4276 | `c01cef212b9d7df6704cb4dc1b457ca15b55bbf53e8bfec36476c76ae86e2944` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T144348Z/combined.txt` | 21377 | `03bd92482a2571f756ee47beed9e6d1391d6ebd09c6bffdc700f13b7b0b6aabd` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T144348Z/run_metadata.json` | 188 | `80e06d8249731c532320459412d214d14d12b72b2056f5dec9f897139c0844c1` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T144348Z/summary.txt` | 180 | `7c1e6982152b5c49d4f9832e653d2d86b2c572bc7675a53b3389bf1def157e9a` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T144348Z/unittest_stderr_time.txt` | 17101 | `116ed66d3f5a28d5f643050877737c02309e4bed054a454e9144bc91e7425672` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/offline_tests_20260717T144348Z/unittest_stdout.txt` | 4276 | `c01cef212b9d7df6704cb4dc1b457ca15b55bbf53e8bfec36476c76ae86e2944` | OFFLINE_UNITTEST_RUN | seal_in_git_permanent | offline_unittest_run |
| `reports/raw/venv_health_post_20260717/capacity_samples.tsv` | 659 | `cc10177489d762378c53d8a446bb0013d1fefae9937228ba25a74862792feb50` | PRIOR_OFFLINE_TEST | seal_in_git_archive | prior_offline_test |
| `reports/raw/venv_health_post_20260717/executor_consumer_samples.txt` | 104 | `46e40fd5b0fdbb9de9463073e071a853ac84696b04716ac1addcc65c8c82d273` | PRIOR_OFFLINE_TEST | seal_in_git_archive | prior_offline_test |
| `reports/raw/venv_health_post_20260717/guard_events.txt` | 41 | `59042f5d4d2247850dfd720a6d28439e460aa9fd0513e34d5a69bc73378dfb42` | PRIOR_OFFLINE_TEST | seal_in_git_archive | prior_offline_test |
| `reports/raw/venv_health_post_20260717/pidstat_samples.txt` | 122863 | `f56917748fd7c9fc07b85ae419807f9735471d77532853d9294487f2fa44e8a9` | PRIOR_OFFLINE_TEST | seal_in_git_archive | prior_offline_test |
| `reports/raw/venv_health_pre_20260717/capacity_samples.tsv` | 651 | `06e5c7322ffd9658ac02df4e267926adb06a61b6fe7e3063a9da556f9cdfc811` | PRIOR_OFFLINE_TEST | seal_in_git_archive | prior_offline_test |
| `reports/raw/venv_health_pre_20260717/executor_consumer_samples.txt` | 104 | `6fd646bd2e2c66ab1b8866630d41bcec479b02df6dccd678247dcba476c922b9` | PRIOR_OFFLINE_TEST | seal_in_git_archive | prior_offline_test |
| `reports/raw/venv_health_pre_20260717/guard_events.txt` | 41 | `6c0f81f82420424a448986c167c3da0dbc944937e2a86088e3635cc05dc63b71` | PRIOR_OFFLINE_TEST | seal_in_git_archive | prior_offline_test |
| `reports/raw/venv_health_pre_20260717/pidstat_samples.txt` | 119399 | `f9c6c3e22b7d200336e96b37f131aa552feaaa71162f6267ac2f848c9dd448bd` | PRIOR_OFFLINE_TEST | seal_in_git_archive | prior_offline_test |

## Policy

- Prefer directory `run_metadata.json` (round_id / artifact_class / source_commit / created_at).
- Legacy timestamp maps are explicit and conservative; unknown stays UNKNOWN/RAW_OTHER.
- Historical raw bytes are not deleted or regenerated by this script.
